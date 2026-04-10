"""
Monitor LP v3 — prezzi Binance + stato pool in tempo reale
===========================================================
- Mostra prezzo SOL aggiornato ogni 30 secondi
- Calcola composizione SOL/USDC di ogni pool (matematica v3)
- Aggiorna pools.json con lo snapshot solo quando premi Ctrl+C

Uso:
    pip install requests
    python monitor_lp.py

Modifica pools.json per aggiungere/togliere pool o aggiornare il capitale.
"""

import requests
import json
import os
import sys
import signal
import time
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL        = "SOLUSDT"
INTERVAL_SEC  = 30
POOLS_FILE    = "pools.json"

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
# COLORI ANSI
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
# GESTIONE pools.json
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
# RENDERING
# ─────────────────────────────────────────────────────────────────────────────

def render_bar(pct_pos, width=40):
    pct  = max(0, min(100, pct_pos))
    pos  = int(pct / 100 * width)
    bar  = list("─" * width)
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

# ─────────────────────────────────────────────────────────────────────────────
# STAMPA
# ─────────────────────────────────────────────────────────────────────────────

def print_header(price, prev_price, stats, history, tick):
    now    = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    change = stats["change_pct"]
    arrow  = "▲" if change >= 0 else "▼"
    c_col  = GREEN if change >= 0 else RED

    # variazione rispetto al tick precedente
    if prev_price:
        diff     = price - prev_price
        diff_col = GREEN if diff >= 0 else RED
        diff_str = clr(f"  ({diff:+.3f} vs precedente)", diff_col)
    else:
        diff_str = ""

    spin = ["◐", "◓", "◑", "◒"][tick % 4]
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
        bar_w    = 30
        sol_w    = max(1, int(comp["sol_pct"] / 100 * bar_w))
        usdc_w   = max(1, bar_w - sol_w)
        comp_bar = clr("█" * sol_w, CYAN) + clr("█" * usdc_w, YELLOW)
        s_sol = comp["sol"]; s_usdc = comp["usdc"]; s_val = comp["val"]
        print("  " + clr(f"{s_sol:.4f} SOL", CYAN) + "  +  " +
              clr(f"${s_usdc:,.2f} USDC", YELLOW) + "  =  " +
              clr(f"${s_val:,.2f}", WHITE))
        print(f"  SOL {comp['sol_pct']:.0f}% {comp_bar} {comp['usdc_pct']:.0f}% USDC")

    capital  = pool.get("capital")
    p_open   = pool.get("p_open", "—")
    opened   = pool.get("opened_at", "")
    note     = pool.get("note", "")
    if capital:
        print(f"  {clr(f'capitale ${capital:,.2f}  p_open ${p_open}  {opened}  {note}', DIM)}")

def print_footer(tick, interval, saved):
    saved_str = clr("  (snapshot salvato al Ctrl+C)", DIM)
    print()
    print(clr("─" * 62, DIM))
    print(clr(f"  Aggiornamento ogni {interval}s  |  Ctrl+C per salvare ed uscire", DIM))
    print(clr("═" * 62, DIM))

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main():
    pools      = load_pools()
    history    = []
    tick       = 0
    prev_price = None

    # ── Ctrl+C / SIGTERM: salva e chiude ──────────────────────────────────────
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
                    pool["balance_sol"]   = round(comp["sol"],   6)
                    pool["balance_usdc"]  = round(comp["usdc"],  4)
                    pool["balance_value"] = round(comp["val"],   4)
                    pool["sol_pct"]       = round(comp["sol_pct"],  1)
                    pool["usdc_pct"]      = round(comp["usdc_pct"], 1)
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

            print_footer(tick, INTERVAL_SEC, saved=False)
            prev_price = price

        except requests.exceptions.RequestException as e:
            print(clr(f"\n  Errore di rete: {e} — riprovo...\n", RED))
        except Exception as e:
            print(clr(f"\n  Errore: {e}\n", RED))

        tick += 1
        time.sleep(INTERVAL_SEC)

# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
