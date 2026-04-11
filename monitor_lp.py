"""
Monitor LP v3 — prezzi Binance + stato pool in tempo reale
===========================================================

Modalità interattiva (default):
    python monitor_lp.py

Modalità daemon (nessun output video, notifiche Telegram):
    python monitor_lp.py --daemon

In modalità daemon:
  - Ogni 120 campioni (= 1 ora a 30s) invia un riepilogo di tutti i pool
  - Appena un pool esce dal range invia un'allerta con bottone ACK
  - Se il bottone ACK non viene premuto entro 10 campioni (5 min) ripete l'allerta
  - Legge token e chat_id da monitor_lp.env

Configurazione bot (file monitor_lp.env):
    TELEGRAM_TOKEN=123456:ABC...
    TELEGRAM_CHAT_ID=-100123456789

Dipendenze:
    pip install requests python-telegram-bot==20.*
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from datetime import datetime

import requests

# ── Import telegram (solo se disponibile) ────────────────────────────────────
try:
    from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL            = "SOLUSDT"
INTERVAL_SEC      = 30
POOLS_FILE        = "pools.json"
ENV_FILE          = "monitor_lp.env"
LOG_DIR           = "log"
LOG_FILE          = os.path.join(LOG_DIR, "monitor_lp_daemon.log")

SUMMARY_EVERY     = 120   # campioni → 1 ora
ACK_TIMEOUT       = 10    # campioni → 5 min per premere ACK prima del reminder

DEFAULT_POOLS = [
    {
        "name":      "Pool A — largo",
        "p_min":     70.0,
        "p_max":     102.0,
        "capital":   1000.0,
        "p_open":    86.0,
        "opened_at": "2026-01-01",
        "note":      "range largo"
    },
    {
        "name":      "Pool B — stretto",
        "p_min":     80.3,
        "p_max":     88.8,
        "capital":   1000.0,
        "p_open":    84.5,
        "opened_at": "2026-02-01",
        "note":      "range stretto"
    },
]

# ─────────────────────────────────────────────────────────────────────────────
# COLORI ANSI (solo modalità interattiva)
# ─────────────────────────────────────────────────────────────────────────────

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
WHITE  = "\033[97m"

def clr(text, color): return f"{color}{text}{RESET}"

# ─────────────────────────────────────────────────────────────────────────────
# ENV LOADER
# ─────────────────────────────────────────────────────────────────────────────

def load_env(path=ENV_FILE):
    """Legge key=value da monitor_lp.env senza dipendenze esterne."""
    env = {}
    if not os.path.exists(path):
        return env
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

# ─────────────────────────────────────────────────────────────────────────────
# POOLS JSON
# ─────────────────────────────────────────────────────────────────────────────

def load_pools():
    if not os.path.exists(POOLS_FILE):
        print(clr(f"  {POOLS_FILE} non trovato — creo il template di default.", YELLOW))
        save_pools(DEFAULT_POOLS)
    with open(POOLS_FILE) as f:
        return json.load(f)

def save_pools(pools):
    with open(POOLS_FILE, "w") as f:
        json.dump(pools, f, indent=2, ensure_ascii=False)

# ─────────────────────────────────────────────────────────────────────────────
# MATEMATICA V3
# ─────────────────────────────────────────────────────────────────────────────

def calc_L(capital, p_open, p_min, p_max):
    p  = max(p_min, min(p_max, p_open))
    sp = p**0.5; sp_min = p_min**0.5; sp_max = p_max**0.5
    sol_per_L  = max(0.0, 1/sp - 1/sp_max)
    usdc_per_L = max(0.0, sp - sp_min)
    val = sol_per_L * p_open + usdc_per_L
    return capital / val if val > 0 else 0.0

def calc_tokens(L, price, p_min, p_max):
    p  = max(p_min, min(p_max, price))
    sp = p**0.5; sp_min = p_min**0.5; sp_max = p_max**0.5
    sol  = max(0.0, L * (1/sp - 1/sp_max)) if sp < sp_max else 0.0
    usdc = max(0.0, L * (sp - sp_min))     if sp > sp_min else 0.0
    return sol, usdc

def pool_composition(pool, price):
    capital = pool.get("capital")
    p_open  = pool.get("p_open")
    p_min   = pool["p_min"]
    p_max   = pool["p_max"]
    if not capital or not p_open:
        return None
    L         = calc_L(capital, p_open, p_min, p_max)
    sol, usdc = calc_tokens(L, price, p_min, p_max)
    val       = sol * price + usdc
    return {
        "sol":      sol,
        "usdc":     usdc,
        "val":      val,
        "sol_pct":  sol * price / val * 100 if val > 0 else 0,
        "usdc_pct": usdc / val * 100        if val > 0 else 0,
    }

# ─────────────────────────────────────────────────────────────────────────────
# BINANCE API
# ─────────────────────────────────────────────────────────────────────────────

def get_price():
    r = requests.get(
        f"https://api.binance.com/api/v3/ticker/price?symbol={SYMBOL}",
        timeout=5
    )
    r.raise_for_status()
    return float(r.json()["price"])

def get_24h_stats():
    r = requests.get(
        f"https://api.binance.com/api/v3/ticker/24hr?symbol={SYMBOL}",
        timeout=5
    )
    r.raise_for_status()
    d = r.json()
    return {
        "change_pct": float(d["priceChangePercent"]),
        "high":       float(d["highPrice"]),
        "low":        float(d["lowPrice"]),
        "volume":     float(d["volume"]),
    }

# ─────────────────────────────────────────────────────────────────────────────
# ANALISI POOL
# ─────────────────────────────────────────────────────────────────────────────

def pool_status(pool, price):
    p_min = pool["p_min"]
    p_max = pool["p_max"]
    width = p_max - p_min
    pct_pos  = (price - p_min) / width * 100
    dist_min = price - p_min
    dist_max = p_max - price
    return {
        "in_range": p_min <= price <= p_max,
        "pct_pos":  pct_pos,
        "dist_min": dist_min,
        "dist_max": dist_max,
        "pct_min":  dist_min / width * 100,
        "pct_max":  dist_max / width * 100,
        "width":    width,
    }

# ─────────────────────────────────────────────────────────────────────────────
# RENDERING TERMINALE
# ─────────────────────────────────────────────────────────────────────────────

def render_bar(pct_pos, width=40):
    pct = max(0, min(100, pct_pos))
    pos = int(pct / 100 * width)
    bar = list("─" * width)
    for i in range(int(0.40*width), int(0.60*width)):
        bar[i] = "·"
    bar[pos] = "█"
    return "[" + "".join(bar) + "]"

def render_sparkline(history, width=24):
    if len(history) < 2:
        return ""
    mn = min(history); mx = max(history)
    if mx == mn:
        return "─" * width
    chars = " ▁▂▃▄▅▆▇█"
    return "".join(
        chars[min(int((p-mn)/(mx-mn)*8), 8)]
        for p in history[-width:]
    )

def clear_screen():
    os.system("cls" if os.name == "nt" else "clear")

def print_header(price, prev_price, stats, history, tick):
    now    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    change = stats["change_pct"]
    arrow  = "▲" if change >= 0 else "▼"
    c_col  = GREEN if change >= 0 else RED
    if prev_price:
        diff     = price - prev_price
        diff_col = GREEN if diff >= 0 else RED
        diff_str = clr(f"  ({diff:+.3f} vs precedente)", diff_col)
    else:
        diff_str = ""
    spin  = ["◐", "◓", "◑", "◒"][tick % 4]
    spark = render_sparkline(history)
    print()
    print(clr("═" * 62, DIM))
    print(f"  {clr('SOL/USDT', BOLD)}   "
          f"{clr(f'${price:,.3f}', WHITE + BOLD)}"
          f"{diff_str}   "
          f"{clr(f'{arrow} {abs(change):.2f}% 24h', c_col)}   "
          f"{clr(now, DIM)}  {spin}")
    h = stats["high"]; lo = stats["low"]; vol = stats["volume"]
    print("  24h  " +
          "H: " + clr(f"${h:,.3f}", GREEN) + "  " +
          "L: " + clr(f"${lo:,.3f}", RED) + "  " +
          "Vol: " + clr(f"{vol:,.0f} SOL", DIM))
    if spark:
        print(f"  Ultimi {len(history)} campioni:  {clr(spark, CYAN)}"
              f"  {clr(f'min ${min(history):,.2f}  max ${max(history):,.2f}', DIM)}")
    print(clr("─" * 62, DIM))

def print_pool(pool, price, s, comp):
    name  = pool["name"]
    p_min = pool["p_min"]
    p_max = pool["p_max"]
    mid   = (p_min + p_max) / 2
    in_range = s["in_range"]
    if in_range:
        status_str = clr("● IN RANGE", GREEN + BOLD)
    elif price < p_min:
        status_str = clr("▼ SOTTO RANGE", RED + BOLD)
    else:
        status_str = clr("▲ SOPRA RANGE", YELLOW + BOLD)
    bar = render_bar(s["pct_pos"])
    print()
    print(f"  {clr(name, BOLD + CYAN)}")
    wid = s["width"]; half_pct = (wid / 2) / mid * 100
    print("  " + clr(f"${p_min:,.2f}", DIM) + " ──── " +
          clr(f"${mid:,.2f}", DIM) + " ──── " +
          clr(f"${p_max:,.2f}", DIM) + "   " +
          clr(f"larghezza ${wid:,.2f}  (±{half_pct:.1f}% dal centro)", DIM))
    print(f"  {bar}  {status_str}")
    if in_range:
        warn_lo = s["pct_min"] < 10
        warn_hi = s["pct_max"] < 10
        dm = s["dist_min"]; pm = s["pct_min"]; dx = s["dist_max"]; px = s["pct_max"]; pp = s["pct_pos"]
        print("  dal min: " + clr(f"+${dm:,.2f} ({pm:.1f}%)", RED if warn_lo else DIM) +
              "   al max: " + clr(f"-${dx:,.2f} ({px:.1f}%)", RED if warn_hi else DIM) +
              "   pos: "    + clr(f"{pp:.1f}%", DIM))
        if warn_lo:
            print(f"  {clr('⚠  VICINO AL BORDO MINIMO — valuta ribilancio', RED + BOLD)}")
        if warn_hi:
            print(f"  {clr('⚠  VICINO AL BORDO MASSIMO — valuta ribilancio', YELLOW + BOLD)}")
    else:
        if price < p_min:
            gap = p_min - price
            print(f"  Mancano {clr(f'+${gap:,.2f} (+{gap/p_min*100:.1f}%)', RED)} per rientrare")
        else:
            gap = price - p_max
            print(f"  Uscito di {clr(f'${gap:,.2f} ({gap/p_max*100:.1f}%)', YELLOW)} sopra il max")
        print(f"  {clr('→ Valuta se ribilanciare il pool', YELLOW)}")
    if comp:
        bar_w  = 30
        sol_w  = max(1, int(comp["sol_pct"] / 100 * bar_w))
        usdc_w = max(1, bar_w - sol_w)
        comp_bar = clr("█" * sol_w, CYAN) + clr("█" * usdc_w, YELLOW)
        print("  " + clr(f"{comp['sol']:.4f} SOL", CYAN) + "  +  " +
              clr(f"${comp['usdc']:,.2f} USDC", YELLOW) + "  =  " +
              clr(f"${comp['val']:,.2f}", WHITE))
        print(f"  SOL {comp['sol_pct']:.0f}% {comp_bar} {comp['usdc_pct']:.0f}% USDC")
    capital = pool.get("capital")
    p_open  = pool.get("p_open", "—")
    opened  = pool.get("opened_at", "")
    note    = pool.get("note", "")
    if capital:
        print(f"  {clr(f'capitale ${capital:,.2f}  p_open ${p_open}  {opened}  {note}', DIM)}")

def print_footer(tick, interval):
    print()
    print(clr("─" * 62, DIM))
    print(clr(f"  Aggiornamento ogni {interval}s  |  Ctrl+C per salvare ed uscire", DIM))
    print(clr("═" * 62, DIM))

# ─────────────────────────────────────────────────────────────────────────────
# MESSAGGI TELEGRAM
# ─────────────────────────────────────────────────────────────────────────────

def _emoji_status(in_range, price, p_min, p_max):
    if in_range:
        return "✅"
    return "🔴" if price < p_min else "🟡"

def build_summary_text(pools, price, stats):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"📊 *Riepilogo orario LP*",
        f"🕐 {now}",
        f"💰 SOL/USDT: *${price:,.3f}*  ({stats['change_pct']:+.2f}% 24h)",
        f"📈 H: ${stats['high']:,.2f}  L: ${stats['low']:,.2f}",
        "",
    ]
    for pool in pools:
        s    = pool_status(pool, price)
        comp = pool_composition(pool, price)
        em   = _emoji_status(s["in_range"], price, pool["p_min"], pool["p_max"])
        lines.append(f"{em} *{pool['name']}*")
        lines.append(f"   Range: ${pool['p_min']:,.2f} — ${pool['p_max']:,.2f}")
        if s["in_range"]:
            lines.append(f"   Posizione: {s['pct_pos']:.1f}%  |  "
                         f"↓ ${s['dist_min']:,.2f} ({s['pct_min']:.1f}%)  "
                         f"↑ ${s['dist_max']:,.2f} ({s['pct_max']:.1f}%)")
            if s["pct_min"] < 10:
                lines.append("   ⚠️ Vicino al bordo minimo!")
            if s["pct_max"] < 10:
                lines.append("   ⚠️ Vicino al bordo massimo!")
        else:
            direction = "sotto" if price < pool["p_min"] else "sopra"
            gap = abs(pool["p_min"] - price) if price < pool["p_min"] else abs(price - pool["p_max"])
            lines.append(f"   🚨 FUORI RANGE ({direction})  gap: ${gap:,.2f}")
        if comp:
            lines.append(f"   💼 {comp['sol']:.4f} SOL + ${comp['usdc']:,.2f} USDC = *${comp['val']:,.2f}*")
        lines.append("")
    return "\n".join(lines)

def build_alert_text(pool, price, tick_ts):
    s    = pool_status(pool, price)
    comp = pool_composition(pool, price)
    direction = "⬇️ SOTTO" if price < pool["p_min"] else "⬆️ SOPRA"
    gap = abs(pool["p_min"] - price) if price < pool["p_min"] else abs(price - pool["p_max"])
    lines = [
        f"🚨 *ALLERTA POOL USCITO DAL RANGE*",
        f"",
        f"🏊 *{pool['name']}*",
        f"📍 SOL: *${price:,.3f}*  →  {direction} RANGE",
        f"📏 Range: ${pool['p_min']:,.2f} — ${pool['p_max']:,.2f}",
        f"📐 Gap dal bordo: *${gap:,.2f}*",
    ]
    if comp:
        lines += [
            f"",
            f"💼 Composizione attuale:",
            f"   {comp['sol']:.4f} SOL + ${comp['usdc']:,.2f} USDC = *${comp['val']:,.2f}*",
        ]
    lines += [
        f"",
        f"🕐 {datetime.now().strftime('%H:%M:%S')}",
        f"",
        f"_Premi ACK per confermare. Se non confermato, ripeto tra 5 minuti._",
    ]
    return "\n".join(lines)

# ─────────────────────────────────────────────────────────────────────────────
# DAEMON STATE
# ─────────────────────────────────────────────────────────────────────────────

class AlertState:
    """Traccia lo stato degli alert per ogni pool (per il meccanismo ACK)."""
    def __init__(self):
        # pool_name → {"msg_id": int, "sent_at_tick": int, "acked": bool}
        self._alerts: dict = {}

    def is_pending(self, pool_name: str) -> bool:
        a = self._alerts.get(pool_name)
        return a is not None and not a["acked"]

    def set_alert(self, pool_name: str, msg_id: int, tick: int):
        self._alerts[pool_name] = {"msg_id": msg_id, "sent_at_tick": tick, "acked": False}

    def ack(self, pool_name: str):
        if pool_name in self._alerts:
            self._alerts[pool_name]["acked"] = True

    def needs_repeat(self, pool_name: str, current_tick: int) -> bool:
        a = self._alerts.get(pool_name)
        if a is None or a["acked"]:
            return False
        return (current_tick - a["sent_at_tick"]) >= ACK_TIMEOUT

    def clear(self, pool_name: str):
        self._alerts.pop(pool_name, None)

    def update_tick(self, pool_name: str, tick: int):
        if pool_name in self._alerts:
            self._alerts[pool_name]["sent_at_tick"] = tick

# ─────────────────────────────────────────────────────────────────────────────
# DAEMON MAIN LOOP
# ─────────────────────────────────────────────────────────────────────────────

async def daemon_loop(token: str, chat_id: str | None):
    """Loop principale del daemon: polling Binance + logica notifiche Telegram."""
    os.makedirs(LOG_DIR, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_FILE, encoding="utf-8"),
        ]
    )
    log = logging.getLogger("monitor_lp")
    log.info(f"Log su file: {LOG_FILE}")
    if not chat_id:
        log.warning("TELEGRAM_CHAT_ID non configurato — notifiche disabilitate. Manda /start al bot per ottenerlo.")

    # ── Setup bot Telegram con handler per il bottone ACK ───────────────────
    alert_state = AlertState()

    async def handle_ack(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        pool_name = query.data  # il callback_data è il nome del pool
        alert_state.ack(pool_name)
        await query.edit_message_text(
            text=query.message.text + f"\n\n✅ *ACK ricevuto* alle {datetime.now().strftime('%H:%M:%S')}",
            parse_mode="Markdown"
        )
        log.info(f"ACK ricevuto per pool: {pool_name}")

    async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Risponde a /start con il chat_id — utile per configurare monitor_lp.env."""
        cid = update.effective_chat.id
        uid = update.effective_user.id if update.effective_user else "?"
        await update.message.reply_text(
            f"👋 *Monitor LP daemon attivo!*\n\n"
            f"🆔 *Chat ID:* `{cid}`\n"
            f"👤 *User ID:* `{uid}`\n\n"
            f"Copia il Chat ID e incollalo in `monitor_lp.env` come:\n"
            f"`TELEGRAM_CHAT_ID={cid}`",
            parse_mode="Markdown"
        )
        log.info(f"/start ricevuto da user {uid} in chat {cid}")

    from telegram.ext import CommandHandler

    app = (
        Application.builder()
        .token(token)
        .build()
    )
    app.add_handler(CommandHandler("start", handle_start))
    app.add_handler(CallbackQueryHandler(handle_ack))

    await app.initialize()
    await app.start()
    # Avvia il polling in background per ricevere i callback ACK
    await app.updater.start_polling(drop_pending_updates=True)

    bot: Bot = app.bot

    async def send_summary(pools, price, stats):
        if not chat_id:
            log.info("Riepilogo orario (chat_id non configurato — nessun invio).")
            return
        text = build_summary_text(pools, price, stats)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown"
        )
        log.info("Riepilogo orario inviato.")

    async def send_alert(pool, price, tick) -> int | None:
        pool_name = pool["name"]
        if not chat_id:
            log.warning(f"ALLERTA pool '{pool_name}' — chat_id non configurato, nessun invio.")
            return None
        text = build_alert_text(pool, price, tick)
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ACK — Ho capito", callback_data=pool_name)
        ]])
        msg = await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        log.warning(f"ALLERTA inviata per pool '{pool_name}' (msg_id={msg.message_id})")
        return msg.message_id

    # ── Stato precedente per rilevare uscite dal range ──────────────────────
    # pool_name → True/False (era in range al tick precedente?)
    prev_in_range: dict = {}

    pools    = load_pools()
    history  = []
    tick     = 0

    log.info("Daemon avviato.")

    # Invia messaggio di avvio (solo se chat_id configurato)
    if chat_id:
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🟢 *Monitor LP daemon avviato*\n"
                f"Pool monitorati: {len(pools)}\n"
                f"Intervallo: {INTERVAL_SEC}s\n"
                f"Riepilogo ogni: {SUMMARY_EVERY * INTERVAL_SEC // 60} minuti\n"
                f"Timeout ACK: {ACK_TIMEOUT * INTERVAL_SEC // 60} minuti"
            ),
            parse_mode="Markdown"
        )
    else:
        log.info("Avvio senza chat_id — manda /start al bot per configurarlo.")

    try:
        while True:
            loop_start = time.monotonic()

            try:
                price = get_price()
                stats = get_24h_stats()
                history.append(price)
                if len(history) > SUMMARY_EVERY:
                    history = history[-SUMMARY_EVERY:]
            except requests.exceptions.RequestException as e:
                log.error(f"Errore di rete: {e} — salto tick {tick}")
                await asyncio.sleep(INTERVAL_SEC)
                tick += 1
                continue

            # ── Controlla ogni pool ──────────────────────────────────────────
            for pool in pools:
                name = pool["name"]
                s    = pool_status(pool, price)
                currently_in = s["in_range"]
                was_in       = prev_in_range.get(name, True)  # al primo tick assume in range

                # Pool appena uscito dal range → allerta immediata
                if was_in and not currently_in:
                    msg_id = await send_alert(pool, price, tick)
                    if msg_id is not None:
                        alert_state.set_alert(name, msg_id, tick)

                # Pool è fuori range: controlla se serve ripetere l'allerta
                elif not currently_in and alert_state.needs_repeat(name, tick):
                    log.warning(f"ACK non ricevuto per '{name}' — ripeto allerta.")
                    msg_id = await send_alert(pool, price, tick)
                    if msg_id is not None:
                        alert_state.update_tick(name, tick)

                # Pool rientrato nel range → pulisce lo stato alert
                elif currently_in and not was_in:
                    alert_state.clear(name)
                    if chat_id:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=(
                                f"✅ *{name}* è rientrato nel range!\n"
                                f"💰 SOL: ${price:,.3f}  "
                                f"(range ${pool['p_min']:,.2f} — ${pool['p_max']:,.2f})"
                            ),
                            parse_mode="Markdown"
                        )
                    log.info(f"Pool '{name}' rientrato nel range.")

                prev_in_range[name] = currently_in

                # Aggiorna snapshot nel json
                comp = pool_composition(pool, price)
                pool["last_price"]    = round(price, 4)
                pool["last_update"]   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                pool["in_range"]      = currently_in
                pool["price_pct_pos"] = round(s["pct_pos"], 2)
                if comp:
                    pool["balance_sol"]   = round(comp["sol"],      6)
                    pool["balance_usdc"]  = round(comp["usdc"],     4)
                    pool["balance_value"] = round(comp["val"],       4)
                    pool["sol_pct"]       = round(comp["sol_pct"],   1)
                    pool["usdc_pct"]      = round(comp["usdc_pct"],  1)

            # ── Riepilogo orario ─────────────────────────────────────────────
            if tick > 0 and tick % SUMMARY_EVERY == 0:
                await send_summary(pools, price, stats)
                save_pools(pools)

            tick += 1

            # Sleep preciso per compensare il tempo di esecuzione
            elapsed = time.monotonic() - loop_start
            await asyncio.sleep(max(0, INTERVAL_SEC - elapsed))

    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        save_pools(pools)
        log.info("Daemon fermato. pools.json aggiornato.")
        if chat_id:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text="🔴 *Monitor LP daemon fermato.*",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

# ─────────────────────────────────────────────────────────────────────────────
# MODALITÀ INTERATTIVA
# ─────────────────────────────────────────────────────────────────────────────

def interactive_loop():
    pools      = load_pools()
    history    = []
    tick       = 0
    prev_price = None

    def on_exit(sig=None, frame=None):
        print(clr("\n\n  Salvataggio snapshot...", CYAN))
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if prev_price:
            for pool in pools:
                s    = pool_status(pool, prev_price)
                comp = pool_composition(pool, prev_price)
                pool["last_price"]    = round(prev_price, 4)
                pool["last_update"]   = now_str
                pool["in_range"]      = s["in_range"]
                pool["price_pct_pos"] = round(s["pct_pos"], 2)
                if comp:
                    pool["balance_sol"]   = round(comp["sol"],      6)
                    pool["balance_usdc"]  = round(comp["usdc"],     4)
                    pool["balance_value"] = round(comp["val"],       4)
                    pool["sol_pct"]       = round(comp["sol_pct"],   1)
                    pool["usdc_pct"]      = round(comp["usdc_pct"],  1)
        save_pools(pools)
        print(clr(f"  {POOLS_FILE} aggiornato.", GREEN))
        print(clr("  Monitor fermato. Ciao!\n", CYAN))
        sys.exit(0)

    signal.signal(signal.SIGINT,  on_exit)
    signal.signal(signal.SIGTERM, on_exit)

    print(clr("\n  Monitor LP v3 avviato — prima lettura...\n", CYAN))

    while True:
        try:
            price = get_price()
            stats = get_24h_stats()
            history.append(price)
            if len(history) > 60:
                history.pop(0)

            clear_screen()
            print_header(price, prev_price, stats, history, tick)

            for pool in pools:
                s    = pool_status(pool, price)
                comp = pool_composition(pool, price)
                print_pool(pool, price, s, comp)

            print_footer(tick, INTERVAL_SEC)
            prev_price = price

        except requests.exceptions.RequestException as e:
            print(clr(f"\n  Errore di rete: {e} — riprovo...\n", RED))
        except Exception as e:
            print(clr(f"\n  Errore: {e}\n", RED))

        tick += 1
        time.sleep(INTERVAL_SEC)

# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Monitor LP v3")
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Modalità daemon: nessun output video, notifiche Telegram"
    )
    args = parser.parse_args()

    if args.daemon:
        if not TELEGRAM_AVAILABLE:
            print("ERRORE: python-telegram-bot non installato.")
            print("  pip install 'python-telegram-bot==20.*'")
            sys.exit(1)

        env = load_env(ENV_FILE)
        token   = env.get("TELEGRAM_TOKEN")
        chat_id = env.get("TELEGRAM_CHAT_ID")  # può essere None: il daemon parte lo stesso

        if not token:
            print(f"ERRORE: TELEGRAM_TOKEN mancante in {ENV_FILE}")
            print(f"  Crea il file {ENV_FILE} con almeno:")
            print(f"    TELEGRAM_TOKEN=<il tuo token da @BotFather>")
            print(f"  Il daemon si avvierà; manda /start al bot per ottenere il TELEGRAM_CHAT_ID.")
            sys.exit(1)

        if not chat_id:
            print(f"ATTENZIONE: TELEGRAM_CHAT_ID non configurato in {ENV_FILE}")
            print(f"  Il daemon si avvia in modalità ascolto.")
            print(f"  Manda /start al bot su Telegram per ottenere il tuo Chat ID,")
            print(f"  poi aggiungilo a {ENV_FILE} e riavvia.")

        asyncio.run(daemon_loop(token, chat_id))
    else:
        interactive_loop()

if __name__ == "__main__":
    main()
