"""
Simulatore Uniswap v3 LP — candele reali + fees matematicamente corrette
pip install streamlit plotly pandas numpy
streamlit run sim_pool_v3.py
"""

import json
import os
import streamlit as st
import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import date, time as dtime

st.set_page_config(page_title="Uniswap v3 Pool Simulator", page_icon="🌊", layout="wide")

st.title("🌊 Uniswap v3 — Pool Simulator")
st.caption("Simulazione fees e valore posizione su candele reali SOL/USDC")

# ─────────────────────────────────────────────────────────────────────────────
# SALVATAGGIO / CARICAMENTO CONFIGURAZIONE POOL
# ─────────────────────────────────────────────────────────────────────────────

POOL_CONFIG_FILE = "pool_config.json"

# Tutti i key usati dalla sidebar
_CFG_KEYS = [
    "cfg_csv_path", "cfg_open_date", "cfg_open_time",
    "cfg_p_min", "cfg_p_max",
    "cfg_input_mode", "cfg_sol_init", "cfg_usdc_init",
    "cfg_capital_init", "cfg_p_open_init",
    "cfg_fee_pct",
    "cfg_intra_mode", "cfg_intra_steps", "cfg_zigzag_n",
    "cfg_real_enabled",
    "cfg_real_sol_now", "cfg_real_usdc_now",
    "cfg_real_fees_sol", "cfg_real_fees_usdc", "cfg_real_price_now",
]


def _save_config():
    data = {}
    for k in _CFG_KEYS:
        v = st.session_state.get(k)
        if isinstance(v, date):        # date (non-pandas)
            data[k] = v.isoformat()
        elif isinstance(v, dtime):     # time
            data[k] = v.isoformat()
        else:
            data[k] = v
    with open(POOL_CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _load_config() -> bool:
    if not os.path.exists(POOL_CONFIG_FILE):
        return False
    with open(POOL_CONFIG_FILE) as f:
        data = json.load(f)
    for k, v in data.items():
        if k not in _CFG_KEYS:
            continue
        if k == "cfg_open_date" and v is not None:
            st.session_state[k] = date.fromisoformat(v)
        elif k == "cfg_open_time" and v is not None:
            st.session_state[k] = dtime.fromisoformat(v)
        else:
            st.session_state[k] = v
    return True


# Carica il config al primo avvio (se esiste)
if "pool_config_initialized" not in st.session_state:
    st.session_state["pool_config_initialized"] = True
    _load_config()


# ─────────────────────────────────────────────────────────────────────────────
# MATEMATICA V3
# ─────────────────────────────────────────────────────────────────────────────

def calc_L_from_tokens(sol, usdc, p, p_min, p_max):
    sp, sp_min, sp_max = np.sqrt(p), np.sqrt(p_min), np.sqrt(p_max)
    if p <= p_min:
        d = 1/sp_min - 1/sp_max
        return sol / d if d > 0 else 0.0
    elif p >= p_max:
        d = sp_max - sp_min
        return usdc / d if d > 0 else 0.0
    else:
        Ls = sol * sp * sp_max / (sp_max - sp) if sp_max > sp else 0
        Lu = usdc / (sp - sp_min) if sp > sp_min else 0
        if Ls > 0 and Lu > 0: return (Ls + Lu) / 2
        return Ls or Lu or 0.0

def calc_L_from_capital(capital, p_open, p_min, p_max):
    sp = np.sqrt(max(p_min, min(p_max, p_open)))
    sp_min, sp_max = np.sqrt(p_min), np.sqrt(p_max)
    sol_per_L  = max(0, 1/sp - 1/sp_max)
    usdc_per_L = max(0, sp - sp_min)
    val = sol_per_L * p_open + usdc_per_L
    return capital / val if val > 0 else 0.0

def calc_tokens(L, p, p_min, p_max):
    p_ = max(p_min, min(p_max, p))
    sp, sp_min, sp_max = np.sqrt(p_), np.sqrt(p_min), np.sqrt(p_max)
    sol  = max(0.0, L * (1/sp - 1/sp_max)) if sp < sp_max else 0.0
    usdc = max(0.0, L * (sp - sp_min))     if sp > sp_min else 0.0
    return sol, usdc

def pos_value(L, p, p_min, p_max):
    sol, usdc = calc_tokens(L, p, p_min, p_max)
    return sol * p + usdc

def fees_from_move(L, p_from, p_to, p_min, p_max, fee_tier):
    """
    Fees generate da un movimento di prezzo p_from → p_to.
    Il volume implicito è determinato dalla variazione di composizione del pool.
    Clipping al range [p_min, p_max] per considerare solo la parte in-range.

    ΔY = L × (√p_b - √p_a)           → USDC scambiati
    ΔX = L × (1/√p_a - 1/√p_b)       → SOL scambiati
    fee_usdc = |ΔY| × fee_tier
    fee_sol  = |ΔX| × fee_tier  (convertiamo in USD al prezzo medio)
    """
    pa = max(p_min, min(p_max, p_from))
    pb = max(p_min, min(p_max, p_to))
    if pa == pb:
        return 0.0

    delta_usdc = abs(L * (np.sqrt(pb) - np.sqrt(pa)))
    delta_sol  = abs(L * (1/np.sqrt(pa) - 1/np.sqrt(pb)))
    p_mid      = (pa + pb) / 2

    fee_from_usdc = delta_usdc * fee_tier
    fee_from_sol  = delta_sol  * p_mid * fee_tier
    return fee_from_usdc + fee_from_sol


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR
# ─────────────────────────────────────────────────────────────────────────────


def _estimate_points(mode, steps, zn):
    """Stima il numero di punti totali per candela dato il modello e gli step."""
    base = {
        "Open → Close": 2,
        "Open → High → Low → Close": 4,
        "Open → Low → High → Close": 4,
        "Zigzag H/L alternati": zn + 2,
        "Zigzag L/H alternati": zn + 2,
        "Open → Mid → High → Mid → Low → Mid → Close": 7,
    }.get(mode, 4)
    return (base - 1) * steps + 1

with st.sidebar:
    st.header("⚙️ Parametri")

    # ── Salva / Carica ────────────────────────────────────────────────────────
    col_save, col_load = st.columns(2)
    with col_save:
        if st.button("💾 Salva", width="stretch", help="Salva la configurazione corrente in pool_config.json"):
            _save_config()
            st.success("Salvato!")
    with col_load:
        if st.button("🔄 Carica", width="stretch", help="Ricarica la configurazione da pool_config.json"):
            if _load_config():
                st.success("Caricato!")
                st.rerun()
            else:
                st.warning("Nessun file trovato.")

    if os.path.exists(POOL_CONFIG_FILE):
        mtime = os.path.getmtime(POOL_CONFIG_FILE)
        st.caption(f"Config: `{POOL_CONFIG_FILE}` — {pd.Timestamp(mtime, unit='s').strftime('%d/%m/%Y %H:%M')}")

    st.divider()
    st.subheader("1. File candele")
    csv_path = st.text_input(
        "Percorso file CSV",
        value="SOL_USDT_15m.csv",
        key="cfg_csv_path",
        help="Percorso assoluto o relativo. Es: D:\\dati\\SOL_USDT_15m.csv"
    )

    st.divider()
    st.subheader("2. Data e ora apertura pool")
    open_date = st.date_input("Data apertura", value=None, key="cfg_open_date",
                               help="La simulazione parte da questa data")
    open_time = st.time_input("Ora apertura", value=None, key="cfg_open_time",
                               help="Ora esatta di apertura (UTC)")

    st.divider()
    st.subheader("3. Range del pool")
    p_min = st.number_input("Prezzo min ($)", min_value=1.0, value=105.0, step=1.0,
                             format="%.2f", key="cfg_p_min")
    p_max = st.number_input("Prezzo max ($)", min_value=1.0, value=178.0, step=1.0,
                             format="%.2f", key="cfg_p_max")

    st.divider()
    st.subheader("4. Posizione iniziale")
    input_mode = st.radio("Modalità input",
                           ["Token (SOL + USDC)", "Capitale + prezzo apertura"],
                           key="cfg_input_mode")
    if input_mode == "Token (SOL + USDC)":
        sol_init  = st.number_input("SOL iniziali",  min_value=0.0, value=5.0,   step=0.01,
                                     format="%.4f", key="cfg_sol_init")
        usdc_init = st.number_input("USDC iniziali", min_value=0.0, value=500.0, step=1.0,
                                     format="%.2f", key="cfg_usdc_init")
    else:
        capital_init = st.number_input("Capitale ($)",        min_value=1.0, value=1000.0,
                                        step=10.0, key="cfg_capital_init")
        p_open_init  = st.number_input("Prezzo apertura ($)", min_value=1.0, value=140.0,
                                        step=1.0,  key="cfg_p_open_init")

    st.divider()
    st.subheader("5. Fee tier")
    fee_pct = st.select_slider(
        "Fee tier",
        options=[0.01, 0.04, 0.05, 0.3, 1.0],
        value=0.04,
        format_func=lambda x: f"{x}%",
        key="cfg_fee_pct",
    )
    fee_tier = fee_pct / 100

    st.divider()
    st.subheader("6. Simulazione intra-candela")
    intra_mode = st.radio(
        "Modello percorso prezzo",
        [
            "Open → Close",
            "Open → High → Low → Close",
            "Open → Low → High → Close",
            "Zigzag H/L alternati",
            "Zigzag L/H alternati",
            "Open → Mid → High → Mid → Low → Mid → Close",
        ],
        index=1,
        key="cfg_intra_mode",
    )
    intra_steps = st.slider(
        "Step interpolazione per ogni segmento",
        min_value=1, max_value=30, value=1,
        key="cfg_intra_steps",
        help=(
            "1 = solo i punti chiave (O/H/L/C). "
            "N > 1 = aggiunge N-1 punti intermedi linearmente tra ogni coppia di punti. "
            "Aumenta per simulare piu volume intra-candela e fees piu alte."
        )
    )
    if "Zigzag" in intra_mode:
        zigzag_n = st.slider(
            "Numero di oscillazioni zigzag",
            min_value=2, max_value=30, value=6,
            key="cfg_zigzag_n",
            help="Quante volte il prezzo oscilla H→L (o L→H) dentro la candela."
        )
    else:
        zigzag_n = 4
    st.caption(
        f"Punti totali per candela: ~{_estimate_points(intra_mode, intra_steps, zigzag_n if 'Zigzag' in intra_mode else 4)}  |  "
        "Moto browniano geometrico in arrivo."
    )

    st.divider()
    st.subheader("7. Dati reali pool (opzionale)")
    st.caption("Inserisci i valori reali che vedi su Orca per confrontarli con la simulazione.")
    real_enabled = st.checkbox("Abilita confronto con dati reali", value=False,
                                key="cfg_real_enabled")
    if real_enabled:
        real_sol_now   = st.number_input("SOL attuali nel pool",        min_value=0.0, value=0.0,
                                          step=0.001, format="%.4f", key="cfg_real_sol_now")
        real_usdc_now  = st.number_input("USDC attuali nel pool",       min_value=0.0, value=0.0,
                                          step=0.01,  format="%.2f", key="cfg_real_usdc_now")
        real_fees_sol  = st.number_input("Fees SOL accumulate (reali)", min_value=0.0, value=0.0,
                                          step=0.001, format="%.4f", key="cfg_real_fees_sol")
        real_fees_usdc = st.number_input("Fees USDC accumulate (reali)",min_value=0.0, value=0.0,
                                          step=0.01,  format="%.2f", key="cfg_real_fees_usdc")
        real_price_now = st.number_input("Prezzo SOL attuale ($)",      min_value=1.0, value=79.0,
                                          step=0.5,   format="%.2f", key="cfg_real_price_now")
    else:
        real_sol_now = real_usdc_now = real_fees_sol = real_fees_usdc = real_price_now = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# CARICAMENTO DATI
# ─────────────────────────────────────────────────────────────────────────────

def load_csv(file):
    df = pd.read_csv(file)
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df

if not csv_path or not os.path.exists(csv_path):
    st.info(f"👈 Inserisci il percorso del file CSV nella sidebar. File cercato: {csv_path}")
    st.stop()

df_full = load_csv(csv_path)
if df_full is None or len(df_full) == 0:
    st.error("File CSV vuoto o non leggibile.")
    st.stop()

# Filtro dalla data/ora di apertura in poi
if open_date is not None and open_time is not None:
    open_dt = pd.Timestamp(f"{open_date} {open_time}")
    df = df_full[df_full["timestamp"] >= open_dt].reset_index(drop=True)
    if len(df) == 0:
        st.error(f"Nessuna candela dopo {open_dt}. Controlla la data di apertura.")
        st.stop()
elif open_date is not None:
    df = df_full[df_full["timestamp"].dt.date >= open_date].reset_index(drop=True)
    if len(df) == 0:
        st.error("Nessuna candela dopo la data selezionata.")
        st.stop()
else:
    df = df_full.copy()
    st.warning("⚠️ Nessuna data di apertura inserita — simulazione sull'intero dataset.")

# ─────────────────────────────────────────────────────────────────────────────
# CALCOLO L INIZIALE
# ─────────────────────────────────────────────────────────────────────────────

p_start = float(df["open"].iloc[0])

if input_mode == "Token (SOL + USDC)":
    L = calc_L_from_tokens(sol_init, usdc_init, p_start, p_min, p_max)
    capital_start = sol_init * p_start + usdc_init
else:
    L = calc_L_from_capital(capital_init, p_open_init, p_min, p_max)
    capital_start = capital_init

if L <= 0:
    st.error("Liquidità L = 0. Controlla i parametri: il capitale potrebbe essere fuori range.")
    st.stop()

# ─────────────────────────────────────────────────────────────────────────────
# SIMULAZIONE
# ─────────────────────────────────────────────────────────────────────────────

def interpolate_segment(p_from, p_to, steps):
    """Genera 'steps' punti intermedi tra p_from e p_to (incluso p_to, escluso p_from)."""
    if steps <= 1:
        return [p_to]
    return list(np.linspace(p_from, p_to, steps + 1)[1:])


def get_intra_prices(row, mode, steps=1, zigzag_n=4):
    """
    Restituisce la sequenza di prezzi simulati dentro la candela.
    steps=1 → solo i punti chiave
    steps=N → N punti interpolati linearmente tra ogni coppia di punti chiave
    """
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    mid = (h + l) / 2

    # Costruisci i waypoint del percorso
    if mode == "Open → Close":
        waypoints = [o, c]
    elif mode == "Open → High → Low → Close":
        waypoints = [o, h, l, c]
    elif mode == "Open → Low → High → Close":
        waypoints = [o, l, h, c]
    elif mode == "Zigzag H/L alternati":
        waypoints = [o]
        for i in range(zigzag_n):
            waypoints.append(h if i % 2 == 0 else l)
        waypoints.append(c)
    elif mode == "Zigzag L/H alternati":
        waypoints = [o]
        for i in range(zigzag_n):
            waypoints.append(l if i % 2 == 0 else h)
        waypoints.append(c)
    elif mode == "Open → Mid → High → Mid → Low → Mid → Close":
        waypoints = [o, mid, h, mid, l, mid, c]
    else:
        waypoints = [o, c]

    # Interpola ogni segmento
    path = [waypoints[0]]
    for i in range(len(waypoints) - 1):
        path.extend(interpolate_segment(waypoints[i], waypoints[i+1], steps))
    return path

timestamps   = []
prices_close = []
values       = []
fees_per_bar = []
in_range_pct = []
sol_list     = []
usdc_list    = []

total_fees   = 0.0

for _, row in df.iterrows():
    path = get_intra_prices(row, intra_mode, intra_steps, zigzag_n)
    bar_fees    = 0.0
    bar_in_range = 0

    for i in range(len(path) - 1):
        p_from = path[i]
        p_to   = path[i+1]

        # fees solo se almeno un pezzo del movimento è in range
        if not (p_to < p_min and p_from < p_min) and not (p_to > p_max and p_from > p_max):
            bar_fees    += fees_from_move(L, p_from, p_to, p_min, p_max, fee_tier)
            bar_in_range += 1

    total_fees += bar_fees
    p_close     = float(row["close"])
    val         = pos_value(L, p_close, p_min, p_max)
    sol_now, usdc_now = calc_tokens(L, p_close, p_min, p_max)

    timestamps.append(row["timestamp"])
    prices_close.append(p_close)
    values.append(val)
    fees_per_bar.append(bar_fees)
    in_range_pct.append(1 if p_min <= p_close <= p_max else 0)
    sol_list.append(sol_now)
    usdc_list.append(usdc_now)

results = pd.DataFrame({
    "timestamp":   timestamps,
    "price":       prices_close,
    "pool_value":  values,
    "fees":        fees_per_bar,
    "fees_cumul":  np.cumsum(fees_per_bar),
    "in_range":    in_range_pct,
    "sol":         sol_list,
    "usdc":        usdc_list,
})

# Hold value (tieni i token iniziali senza LP)
sol_hold, usdc_hold = calc_tokens(L, p_start, p_min, p_max)
results["hold_value"] = sol_hold * results["price"] + usdc_hold

# Total value incluse fees
results["total_value"] = results["pool_value"] + results["fees_cumul"]

# ─────────────────────────────────────────────────────────────────────────────
# KPI
# ─────────────────────────────────────────────────────────────────────────────

val_start   = pos_value(L, p_start, p_min, p_max)
val_end     = results["pool_value"].iloc[-1]
val_total   = val_end + total_fees
hold_end    = results["hold_value"].iloc[-1]
il          = val_end - hold_end
il_pct      = il / hold_end * 100 if hold_end > 0 else 0
fee_yield   = total_fees / capital_start * 100
days_total  = (df["timestamp"].iloc[-1] - df["timestamp"].iloc[0]).days or 1
apr         = fee_yield / days_total * 365
pct_in_range = results["in_range"].mean() * 100
bars_total  = len(results)

st.subheader("📊 KPI simulazione")
c1,c2,c3,c4,c5,c6 = st.columns(6)
with c1: st.metric("Valore pool finale",    f"${val_end:,.2f}",    delta=f"{val_end-val_start:+,.2f}$")
with c2: st.metric("Fees accumulate",       f"${total_fees:,.2f}", delta=f"APR ~{apr:.1f}%")
with c3: st.metric("Valore totale (pool+fees)", f"${val_total:,.2f}", delta=f"{val_total-capital_start:+,.2f}$ vs apertura")
with c4: st.metric("Impermanent Loss",      f"${il:,.2f}",         delta=f"{il_pct:+.2f}% vs hold")
with c5: st.metric("Tempo in range",        f"{pct_in_range:.1f}%")
with c6: st.metric("Candele analizzate",    f"{bars_total:,}")


# ─────────────────────────────────────────────────────────────────────────────
# CONFRONTO CON DATI REALI
# ─────────────────────────────────────────────────────────────────────────────

if real_enabled:
    st.subheader("🔍 Confronto simulazione vs reale")

    # Valori reali
    real_pool_value  = real_sol_now  * real_price_now + real_usdc_now
    real_fees_value  = real_fees_sol * real_price_now + real_fees_usdc
    real_total       = real_pool_value + real_fees_value

    # Valori simulati all'ultimo step
    sim_pool_last    = results["pool_value"].iloc[-1]
    sim_fees_last    = results["fees_cumul"].iloc[-1]
    sim_total_last   = sim_pool_last + sim_fees_last
    sim_sol_last     = results["sol"].iloc[-1]
    sim_usdc_last    = results["usdc"].iloc[-1]

    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("**Simulazione**")
        st.metric("SOL (sim)",          f"{sim_sol_last:.4f}",    delta=f"≈ ${sim_sol_last*real_price_now:,.2f}")
        st.metric("USDC (sim)",         f"${sim_usdc_last:,.2f}")
        st.metric("Fees cumulate (sim)",f"${sim_fees_last:,.4f}")
        st.metric("Totale (sim)",       f"${sim_total_last:,.2f}")

    with c2:
        st.markdown("**Reale (Orca)**")
        st.metric("SOL (reale)",         f"{real_sol_now:.4f}",   delta=f"≈ ${real_sol_now*real_price_now:,.2f}")
        st.metric("USDC (reale)",        f"${real_usdc_now:,.2f}")
        st.metric("Fees cumulate (reale)",f"${real_fees_value:,.4f}")
        st.metric("Totale (reale)",      f"${real_total:,.2f}")

    with c3:
        st.markdown("**Scarto sim − reale**")
        d_sol   = sim_sol_last  - real_sol_now
        d_usdc  = sim_usdc_last - real_usdc_now
        d_fees  = sim_fees_last - real_fees_value
        d_total = sim_total_last - real_total
        st.metric("ΔSOL",    f"{d_sol:+.4f}",   delta=f"≈ ${d_sol*real_price_now:+,.2f}")
        st.metric("ΔUSDC",   f"${d_usdc:+,.2f}")
        st.metric("ΔFees",   f"${d_fees:+,.4f}")
        st.metric("ΔTotale", f"${d_total:+,.2f}",
                  delta=f"{d_total/real_total*100:+.2f}% vs reale" if real_total > 0 else "n/d")

    # Nota interpretativa
    if abs(d_fees) > real_fees_value * 0.2:
        st.info(
            "**Scarto fees > 20%:** probabile causa — la simulazione usa solo open/high/low/close "
            "e non cattura i movimenti intra-candela reali. "
            "Il moto browniano geometrico (prossima versione) ridurrà questo scarto stimando "
            "il volume reale dentro ogni candela."
        )
    else:
        st.success("Scarto fees contenuto — la simulazione è una buona approssimazione del reale.")

    st.divider()

st.subheader("📈 Andamento simulazione")

fig = make_subplots(
    rows=4, cols=1,
    shared_xaxes=True,
    row_heights=[0.35, 0.25, 0.25, 0.15],
    vertical_spacing=0.04,
    subplot_titles=("Prezzo SOL", "Valore posizione ($)", "Fees cumulate ($)", "SOL / USDC nel pool")
)

# 1. Prezzo candele
fig.add_trace(go.Candlestick(
    x=df["timestamp"], open=df["open"], high=df["high"],
    low=df["low"], close=df["close"], name="SOL/USDC",
    increasing_line_color="#34d399", increasing_fillcolor="#34d399",
    decreasing_line_color="#f87171", decreasing_fillcolor="#f87171",
    whiskerwidth=1,
), row=1, col=1)
fig.add_hline(y=p_min, line_dash="dash", line_color="#4f9cf9",  line_width=1,
              annotation_text=f"Min ${p_min}", row=1, col=1)
fig.add_hline(y=p_max, line_dash="dash", line_color="#4f9cf9",  line_width=1,
              annotation_text=f"Max ${p_max}", row=1, col=1)

# 2. Valore pool vs hold vs totale
fig.add_trace(go.Scatter(x=results["timestamp"], y=results["pool_value"],
    name="Pool (solo token)", line=dict(color="#4f9cf9", width=2)), row=2, col=1)
fig.add_trace(go.Scatter(x=results["timestamp"], y=results["total_value"],
    name="Pool + fees", line=dict(color="#34d399", width=2)), row=2, col=1)
fig.add_trace(go.Scatter(x=results["timestamp"], y=results["hold_value"],
    name="Hold SOL", line=dict(color="#fb923c", width=1.5, dash="dot")), row=2, col=1)
fig.add_hline(y=capital_start, line_dash="dash", line_color="#a78bfa", line_width=1,
              annotation_text="Capitale iniziale", row=2, col=1)

# 3. Fees cumulate + per candela
fig.add_trace(go.Scatter(x=results["timestamp"], y=results["fees_cumul"],
    name="Fees cumulate", line=dict(color="#fbbf24", width=2),
    fill="tozeroy", fillcolor="rgba(251,191,36,0.08)"), row=3, col=1)
fig.add_trace(go.Bar(x=results["timestamp"], y=results["fees"],
    name="Fees per candela", marker_color="rgba(251,191,36,0.4)"), row=3, col=1)

# 4. Composizione token
fig.add_trace(go.Scatter(x=results["timestamp"], y=results["sol"],
    name="SOL", line=dict(color="#34d399", width=1.5),
    stackgroup="tokens", fillcolor="rgba(52,211,153,0.3)"), row=4, col=1)
fig.add_trace(go.Scatter(x=results["timestamp"], y=results["usdc"] / results["price"],
    name="USDC (in SOL equiv)", line=dict(color="#4f9cf9", width=1.5),
    stackgroup="tokens", fillcolor="rgba(79,156,249,0.3)"), row=4, col=1)

fig.update_layout(
    height=900,
    plot_bgcolor="#12151c", paper_bgcolor="#0a0c10",
    font=dict(color="#e8eaf0"),
    legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="left", x=0),
    xaxis_rangeslider_visible=False,
    hovermode="x unified",
)
for i in range(1, 5):
    fig.update_xaxes(gridcolor="rgba(255,255,255,0.04)", row=i, col=1)
    fig.update_yaxes(gridcolor="rgba(255,255,255,0.04)", row=i, col=1)

st.plotly_chart(fig, width="stretch")


# ─────────────────────────────────────────────────────────────────────────────
# TABELLA RIEPILOGO MENSILE
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("📅 Riepilogo mensile")

results["month"] = results["timestamp"].dt.to_period("M").astype(str)
monthly = results.groupby("month").agg(
    fees_mese    = ("fees",       "sum"),
    giorni_range = ("in_range",   "mean"),
    prezzo_fine  = ("price",      "last"),
    val_fine     = ("pool_value", "last"),
).reset_index()
monthly["giorni_range"] = (monthly["giorni_range"] * 100).round(1).astype(str) + "%"
monthly["fees_mese"]    = monthly["fees_mese"].map(lambda x: f"${x:,.4f}")
monthly["val_fine"]     = monthly["val_fine"].map(lambda x: f"${x:,.2f}")
monthly["prezzo_fine"]  = monthly["prezzo_fine"].map(lambda x: f"${x:,.2f}")
monthly.columns = ["Mese", "Fees ($)", "% in range", "Prezzo fine", "Valore pool"]
st.dataframe(monthly, width="stretch", hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# EXPORT
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("💾 Esporta risultati")
csv_out = results.drop(columns=["month"], errors="ignore").to_csv(index=False)
st.download_button("⬇️ Scarica CSV simulazione", csv_out, "sim_pool_v3_results.csv", "text/csv")

st.divider()
st.caption("""
**Matematica fees:** ΔY = L·(√P_b − √P_a) → USDC scambiati | ΔX = L·(1/√P_a − 1/√P_b) → SOL scambiati
Fee = (|ΔY| + |ΔX|·P_mid) × fee_tier — indipendente dalla liquidità totale del pool.
Uniswap v3 whitepaper Eq. 6.14–6.16 | Non è consulenza finanziaria.
""")
