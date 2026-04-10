"""
Backtest strategie Uniswap v3 LP con rebalancing automatico
============================================================
Confronta più ampiezze di range sulla stessa serie storica di candele.

Strategia simulata:
  - Apri pool con range ±X% centrato sul prezzo corrente
  - Accumuli fees finché sei in range
  - Quando il prezzo esce dal range → chiudi, riapri centrato sul nuovo prezzo
  - Ogni reopen costa `reopen_cost_usd` (gas + slippage su Solana ~$0.01-0.10)

Benchmark:
  - Hold SOL puro (nessuna azione)

Uso:
    pip install pandas numpy
    python backtest_lp_v3.py
"""

import pandas as pd
import numpy as np
import os
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURAZIONE — modifica qui
# ─────────────────────────────────────────────────────────────────────────────

CSV_PATH       = "SOL_USDT_15m.csv"   # path del file candele
CAPITAL        = 1000.0               # capitale iniziale in USD
FEE_TIER       = 0.04 / 100          # 0.04%
REOPEN_COST    = 0.10                 # costo per ogni reopen ($)

# Ampiezze range da confrontare (±% dal prezzo centrale)
RANGE_WIDTHS   = [2.5, 5, 10, 20, 40]

# Reinvesti le fees nel pool ad ogni reopen (True = compounding, False = fees tenute in cash)
REINVEST_FEES  = True

# Modello intra-candela per il calcolo fees
# Opzioni: "OC", "OHLC", "OLHC", "zigzag"
INTRA_MODE     = "OHLC"
INTRA_STEPS    = 3      # punti interpolati per segmento (1 = solo waypoint)
ZIGZAG_N       = 6      # oscillazioni zigzag (usato solo se INTRA_MODE="zigzag")

# Data inizio simulazione (None = usa tutto il CSV)
START_DATE     = None   # es. "2025-06-01"
END_DATE       = None   # es. "2026-01-01"

# ─────────────────────────────────────────────────────────────────────────────
# MATEMATICA V3
# ─────────────────────────────────────────────────────────────────────────────

def calc_L_from_capital(capital, p_open, p_min, p_max):
    """L dal capitale totale al prezzo di apertura."""
    sp      = np.sqrt(np.clip(p_open, p_min, p_max))
    sp_min  = np.sqrt(p_min)
    sp_max  = np.sqrt(p_max)
    sol_per_L  = max(0.0, 1/sp - 1/sp_max)
    usdc_per_L = max(0.0, sp - sp_min)
    val = sol_per_L * p_open + usdc_per_L
    return capital / val if val > 0 else 0.0


def calc_tokens(L, p, p_min, p_max):
    """SOL e USDC dati L e prezzo."""
    p_  = np.clip(p, p_min, p_max)
    sp  = np.sqrt(p_)
    sol  = max(0.0, L * (1/sp - 1/np.sqrt(p_max))) if sp < np.sqrt(p_max) else 0.0
    usdc = max(0.0, L * (sp - np.sqrt(p_min)))      if sp > np.sqrt(p_min) else 0.0
    return sol, usdc


def pos_value(L, p, p_min, p_max):
    sol, usdc = calc_tokens(L, p, p_min, p_max)
    return sol * p + usdc


def fees_segment(L, pa, pb, p_min, p_max, fee_tier):
    """Fees da un singolo movimento di prezzo pa→pb (clippato al range)."""
    a = np.clip(pa, p_min, p_max)
    b = np.clip(pb, p_min, p_max)
    if a == b:
        return 0.0
    d_usdc = abs(L * (np.sqrt(b) - np.sqrt(a)))
    d_sol  = abs(L * (1/np.sqrt(a) - 1/np.sqrt(b)))
    p_mid  = (a + b) / 2
    return (d_usdc + d_sol * p_mid) * fee_tier


def interpolate_segment(p_from, p_to, steps):
    if steps <= 1:
        return [p_to]
    return list(np.linspace(p_from, p_to, steps + 1)[1:])


def build_price_path(row, mode, steps, zigzag_n):
    """Sequenza di prezzi intra-candela."""
    o, h, l, c = row.open, row.high, row.low, row.close
    mid = (h + l) / 2

    if mode == "OC":
        waypoints = [o, c]
    elif mode == "OHLC":
        waypoints = [o, h, l, c]
    elif mode == "OLHC":
        waypoints = [o, l, h, c]
    elif mode == "zigzag":
        waypoints = [o]
        for i in range(zigzag_n):
            waypoints.append(h if i % 2 == 0 else l)
        waypoints.append(c)
    elif mode == "mid":
        waypoints = [o, mid, h, mid, l, mid, c]
    else:
        waypoints = [o, c]

    path = [waypoints[0]]
    for i in range(len(waypoints) - 1):
        path.extend(interpolate_segment(waypoints[i], waypoints[i+1], steps))
    return path


# ─────────────────────────────────────────────────────────────────────────────
# CARICAMENTO CSV
# ─────────────────────────────────────────────────────────────────────────────

def load_candles(path, start=None, end=None):
    if not os.path.exists(path):
        raise FileNotFoundError(f"File non trovato: {path}")
    df = pd.read_csv(path)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    if start:
        df = df[df["timestamp"] >= pd.Timestamp(start)]
    if end:
        df = df[df["timestamp"] <= pd.Timestamp(end)]
    df = df.reset_index(drop=True)
    print(f"  Candele caricate : {len(df):,}")
    print(f"  Da               : {df['timestamp'].iloc[0]}")
    print(f"  A                : {df['timestamp'].iloc[-1]}")
    days = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days
    print(f"  Giorni totali    : {days}")
    return df, days


# ─────────────────────────────────────────────────────────────────────────────
# SIMULAZIONE SINGOLA STRATEGIA
# ─────────────────────────────────────────────────────────────────────────────

def simulate_strategy(df, capital, half_width_pct, fee_tier,
                      reopen_cost, intra_mode, intra_steps, zigzag_n,
                      reinvest_fees=True):
    """
    Simula la strategia con range ±half_width_pct% sul dataset df.
    Restituisce un dict con le metriche finali.
    """
    p_first = float(df["open"].iloc[0])
    factor  = half_width_pct / 100.0

    # Stato corrente
    p_min   = p_first * (1 - factor)
    p_max   = p_first * (1 + factor)
    pool_val = capital  # valore netto del pool (al netto dei costi reopen)
    L        = calc_L_from_capital(pool_val, p_first, p_min, p_max)

    fees_periodo   = 0.0   # fees nel periodo corrente (tra un reopen e l'altro)
    fees_storiche  = 0.0   # fees totali generate in tutto il backtest
    cash_fees      = 0.0   # fees tenute in cash (solo se reinvest_fees=False)
    total_reopen_costs = 0.0
    n_reopens      = 0
    bars_in_range  = 0
    bars_total     = len(df)

    # Per il calcolo IL: SOL e USDC iniziali per il benchmark hold
    sol_hold, usdc_hold = calc_tokens(L, p_first, p_min, p_max)

    monthly = {}

    for _, row in df.iterrows():
        path = build_price_path(row, intra_mode, intra_steps, zigzag_n)
        bar_fees = 0.0

        for i in range(len(path) - 1):
            p_from = path[i]
            p_to   = path[i+1]
            if not (p_to < p_min and p_from < p_min) and \
               not (p_to > p_max and p_from > p_max):
                bar_fees += fees_segment(L, p_from, p_to, p_min, p_max, fee_tier)

        fees_periodo  += bar_fees
        fees_storiche += bar_fees   # contatore cumulativo mai azzerato

        p_close = float(row["close"])

        if p_close < p_min or p_close > p_max:
            # Valore pool al prezzo di chiusura (include IL se fuori range)
            val_chiusura = pos_value(L, p_close, p_min, p_max)

            if reinvest_fees:
                # Compounding: reinvesti tutto (pool + fees periodo) nel nuovo range
                pool_val      = val_chiusura + fees_periodo - reopen_cost
                cash_fees    += 0.0   # niente va in cash
            else:
                # Fees tenute in cash: reinvesti solo il valore pool
                cash_fees    += fees_periodo   # fees vanno nel cassetto
                pool_val      = val_chiusura - reopen_cost

            total_reopen_costs += reopen_cost
            n_reopens    += 1
            fees_periodo  = 0.0   # azzera il periodo corrente

            # Protezione: se il pool_val è andato sotto zero (molto improbabile)
            pool_val = max(pool_val, 0.01)

            # Riapri centrato sul nuovo prezzo
            p_min = p_close * (1 - factor)
            p_max = p_close * (1 + factor)
            L     = calc_L_from_capital(pool_val, p_close, p_min, p_max)
        else:
            bars_in_range += 1

        month = row["timestamp"].strftime("%Y-%m")
        if month not in monthly:
            monthly[month] = {"fees": 0.0, "reopens": 0}
        monthly[month]["fees"]    += bar_fees
        monthly[month]["reopens"] += (1 if (p_close < p_min or p_close > p_max) else 0)

    # Valore finale:
    # pool_val = capitale composto da tutti i reopen (include fees storiche reinvestite)
    # fees_periodo = fees dell'ultimo periodo non ancora reinvestite
    p_last   = float(df["close"].iloc[-1])
    val_pool = pos_value(L, p_last, p_min, p_max)
    # fees_periodo = fees ultimo ciclo non ancora reinvestite
    # cash_fees    = fees accumulate in cash (solo se reinvest_fees=False)
    val_tot  = val_pool + fees_periodo + cash_fees

    hold_val = sol_hold * p_last + usdc_hold
    il       = val_tot - hold_val
    il_pct   = il / hold_val * 100 if hold_val > 0 else 0
    net_return     = val_tot - capital
    net_return_pct = net_return / capital * 100

    days_sim = max((df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days, 1)
    fee_apr  = (fees_storiche - total_reopen_costs) / capital * 365 / days_sim * 100

    return {
        "range_pct"          : half_width_pct,
        "label"              : f"±{half_width_pct}%",
        "val_pool_finale"    : val_pool,
        "fees_storiche"      : fees_storiche,
        "fees_periodo"       : fees_periodo,
        "cash_fees"          : cash_fees,
        "reinvest_fees"      : reinvest_fees,
        "valore_totale"      : val_tot,
        "reopen_costs"       : total_reopen_costs,
        "n_reopens"          : n_reopens,
        "net_return"         : net_return,
        "net_return_pct"     : net_return_pct,
        "fee_apr"            : fee_apr,
        "hold_value"         : hold_val,
        "il_usd"             : il,
        "il_pct"             : il_pct,
        "pct_in_range"       : bars_in_range / bars_total * 100,
        "monthly"            : monthly,
    }


# ─────────────────────────────────────────────────────────────────────────────
# STAMPA RISULTATI
# ─────────────────────────────────────────────────────────────────────────────

def col(s, w):
    """Tronca o padda una stringa a larghezza w."""
    s = str(s)
    return s[:w].ljust(w)


def print_summary(results, capital, days):
    W = 10  # larghezza colonne

    headers = ["Range", "Val.Tot $", "Fees tot$", "Fee APR%", "Reopens",
               "Costi $",  "Rend.%", "IL $", "In Range%"]
    sep = "─" * (len(headers) * (W + 1) + 1)

    print()
    print(sep)
    print("  RIEPILOGO STRATEGIE LP v3  —  Capitale iniziale: ${:,.0f}  —  Giorni: {}".format(capital, days))
    print(sep)
    print(" ".join(col(h, W) for h in headers))
    print(sep)

    best_total = max(r["valore_totale"] for r in results)
    best_apr   = max(r["fee_apr"]       for r in results)

    show_cash = any(not r["reinvest_fees"] for r in results)

    for r in results:
        marker = ""
        if abs(r["valore_totale"] - best_total) < 0.01: marker += " ◄ BEST VALORE"
        if abs(r["fee_apr"]       - best_apr)   < 0.01: marker += " ◄ BEST APR"
        row = [
            col(r["label"],                     W),
            col(f"${r['valore_totale']:,.1f}",  W),
            col(f"${r['fees_storiche']:,.2f}",  W),
            col(f"{r['fee_apr']:+.1f}%",        W),
            col(str(r["n_reopens"]),            W),
            col(f"${r['reopen_costs']:,.2f}",   W),
            col(f"{r['net_return_pct']:+.1f}%", W),
            col(f"${r['il_usd']:+,.1f}",        W),
            col(f"{r['pct_in_range']:.0f}%",    W),
        ]
        if show_cash:
            row.append(col(f"${r['cash_fees']:,.2f}", W))
        print(" ".join(row) + marker)

    print(sep)
    # Hold benchmark
    hold_val = results[0]["hold_value"]
    hold_ret = (hold_val - capital) / capital * 100
    print(f"  HOLD SOL puro   →  Valore finale: ${hold_val:,.1f}  ({hold_ret:+.1f}%)")
    print(sep)


def print_monthly(results, top_n=2):
    """Stampa riepilogo mensile per le top N strategie per valore totale."""
    sorted_r = sorted(results, key=lambda x: x["valore_totale"], reverse=True)
    top      = sorted_r[:top_n]

    print()
    print("─" * 70)
    print(f"  DETTAGLIO MENSILE — prime {top_n} strategie per rendimento")
    print("─" * 70)

    for r in top:
        print(f"\n  Strategia {r['label']}  |  Fees storiche: ${r['fees_storiche']:,.2f}  |  Fee APR netto: {r['fee_apr']:+.1f}%  |  Reopens: {r['n_reopens']}")
        print(f"  {'Mese':<10} {'Fees ($)':>10} {'Reopens':>10}")
        print("  " + "─" * 32)
        for month, data in sorted(r["monthly"].items()):
            print(f"  {month:<10} {data['fees']:>10.3f} {data['reopens']:>10}")


def print_params(df):
    print()
    print("═" * 60)
    print("  BACKTEST LP v3 — Strategia range stretto + rebalancing")
    print("═" * 60)
    print(f"  File CSV         : {CSV_PATH}")
    print(f"  Capitale         : ${CAPITAL:,.0f}")
    print(f"  Fee tier         : {FEE_TIER*100:.2f}%")
    print(f"  Costo reopen     : ${REOPEN_COST:.3f}")
    print(f"  Reinvesti fees   : {REINVEST_FEES}  (True=compounding, False=cash)")
    print(f"  Modello intra    : {INTRA_MODE}  (steps={INTRA_STEPS}" +
          (f", zigzag_n={ZIGZAG_N}" if INTRA_MODE == "zigzag" else "") + ")")
    print(f"  Range testati    : {RANGE_WIDTHS}")
    print()


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print()
    print("Caricamento candele...")
    df, days = load_candles(CSV_PATH, START_DATE, END_DATE)

    print_params(df)

    results = []
    for w in RANGE_WIDTHS:
        print(f"  Simulazione ±{w}%...", end="", flush=True)
        r = simulate_strategy(
            df, CAPITAL, w, FEE_TIER,
            REOPEN_COST, INTRA_MODE, INTRA_STEPS, ZIGZAG_N,
            reinvest_fees=REINVEST_FEES
        )
        results.append(r)
        print(f"  fees storiche=${r['fees_storiche']:,.2f}  fee_apr={r['fee_apr']:+.1f}%  reopens={r['n_reopens']}")

    print_summary(results, CAPITAL, days)
    print_monthly(results, top_n=2)

    print()
    print("═" * 60)
    print("  Legenda:")
    print("  Val.Tot  = valore pool + fees accumulate al netto dei costi reopen")
    print("  Reopens  = numero di volte che il range è stato ricentrato")
    print("  Costi    = totale gas/slippage pagati per i reopen")
    print("  IL       = Impermanent Loss vs hold SOL puro")
    print("  In Range = % di candele chiuse dentro il range attivo")
    print("═" * 60)
    print()
