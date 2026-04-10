import streamlit as st
import numpy as np
import plotly.graph_objects as go
import pandas as pd

st.set_page_config(page_title="Uniswap v3 LP Calculator", page_icon="📊", layout="wide")

st.title("📊 Uniswap v3 — LP Calculator")
st.caption("Matematica corretta basata sui token reali del pool (whitepaper Uniswap v3 Eq. 6.29/6.30)")

# ─────────────────────────────────────────────────────────────────────────────
# MATEMATICA V3 CORRETTA
# ─────────────────────────────────────────────────────────────────────────────

def calc_L_from_tokens(sol_amount, usdc_amount, p_curr, p_min, p_max):
    """
    Calcola L dai token reali presenti nel pool.
    Uniswap v3 whitepaper:
      x = L * (1/sqrt(p) - 1/sqrt(pb))   → SOL
      y = L * (sqrt(p) - sqrt(pa))        → USDC
    """
    sp     = np.sqrt(max(p_min, min(p_max, p_curr)))
    sp_min = np.sqrt(p_min)
    sp_max = np.sqrt(p_max)

    if p_curr <= p_min:
        # tutto SOL
        denom = 1/sp_min - 1/sp_max
        return sol_amount / denom if denom > 0 else 0.0

    elif p_curr >= p_max:
        # tutto USDC
        denom = sp_max - sp_min
        return usdc_amount / denom if denom > 0 else 0.0

    else:
        # in range: calcola L da entrambi i token e fai la media
        L_sol  = sol_amount  * sp * sp_max / (sp_max - sp) if (sp_max - sp) > 0 else None
        L_usdc = usdc_amount / (sp - sp_min)               if (sp - sp_min) > 0 else None

        if L_sol and L_usdc:
            return (L_sol + L_usdc) / 2.0
        return L_sol or L_usdc or 0.0


def calc_L_from_capital_at_open(capital, p_open, p_min, p_max):
    """
    Calcola L dal capitale totale investito al prezzo di apertura.
    Valore per unità di L = SOL_per_L * p_open + USDC_per_L
    """
    sp      = np.sqrt(max(p_min, min(p_max, p_open)))
    sp_min  = np.sqrt(p_min)
    sp_max  = np.sqrt(p_max)

    sol_per_L  = (1/sp - 1/sp_max) if sp < sp_max else 0.0
    usdc_per_L = (sp - sp_min)     if sp > sp_min else 0.0
    val_per_L  = sol_per_L * p_open + usdc_per_L
    return capital / val_per_L if val_per_L > 0 else 0.0


def calc_tokens_from_L(L, p_curr, p_min, p_max):
    """Dati L, prezzo e range → restituisce (SOL, USDC)."""
    p      = max(p_min, min(p_max, p_curr))
    sp     = np.sqrt(p)
    sp_min = np.sqrt(p_min)
    sp_max = np.sqrt(p_max)

    sol  = max(0.0, L * (1/sp - 1/sp_max)) if sp < sp_max else 0.0
    usdc = max(0.0, L * (sp - sp_min))     if sp > sp_min else 0.0
    return sol, usdc


def position_value(L, p_target, p_min, p_max):
    """Valore totale USD della posizione al prezzo p_target."""
    sol, usdc = calc_tokens_from_L(L, p_target, p_min, p_max)
    return sol * p_target + usdc


def calc_L_new_from_value(val_now, p_curr, p_min_new, p_max_new):
    """
    Calcola la nuova L assumendo che chiudi la posizione attuale
    e la riapri con val_now come capitale nel nuovo range al prezzo corrente.
    """
    if p_curr <= p_min_new:
        # fuori range → tutto SOL disponibile
        sol_available = val_now / p_curr
        denom = 1/np.sqrt(p_min_new) - 1/np.sqrt(p_max_new)
        return sol_available / denom if denom > 0 else 0.0
    elif p_curr >= p_max_new:
        # sopra range → tutto USDC
        denom = np.sqrt(p_max_new) - np.sqrt(p_min_new)
        return val_now / denom if denom > 0 else 0.0
    else:
        return calc_L_from_capital_at_open(val_now, p_curr, p_min_new, p_max_new)


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR — INPUT
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Dati del pool")

    st.subheader("1. Range originale del pool")
    p_min_orig = st.number_input("Prezzo min originale ($)", min_value=1.0,  value=105.0, step=1.0, format="%.2f")
    p_max_orig = st.number_input("Prezzo max originale ($)", min_value=1.0,  value=178.0, step=1.0, format="%.2f")

    st.subheader("2. Prezzo attuale SOL")
    p_curr = st.number_input("Prezzo attuale ($)", min_value=1.0, value=79.0, step=0.5, format="%.2f")

    st.divider()
    st.subheader("3. Come vuoi inserire la posizione?")
    input_mode = st.radio(
        "Metodo",
        [
            "📍 Token attuali da Orca/Raydium",
            "📅 Prezzo apertura + capitale iniziale",
        ],
        index=0
    )

    if input_mode == "📍 Token attuali da Orca/Raydium":
        st.caption("Vai su Orca → Portfolio → apri la posizione e leggi i token.")
        sol_now  = st.number_input("SOL nel pool ora",  min_value=0.0, value=13.5,  step=0.001, format="%.4f")
        usdc_now = st.number_input("USDC nel pool ora", min_value=0.0, value=0.0,   step=0.01,  format="%.2f")
        fees_sol  = st.number_input("Fees accumulate SOL (opz.)",  min_value=0.0, value=0.0, step=0.001, format="%.4f")
        fees_usdc = st.number_input("Fees accumulate USDC (opz.)", min_value=0.0, value=0.0, step=0.01,  format="%.2f")
    else:
        st.caption("Usa se non ricordi la composizione attuale ma conosci quando hai aperto.")
        p_open  = st.number_input("Prezzo SOL all'apertura ($)", min_value=1.0, value=140.0, step=1.0, format="%.2f")
        capital = st.number_input("Capitale totale investito ($)", min_value=1.0, value=1100.0, step=10.0, format="%.2f")
        fees_sol  = st.number_input("Fees accumulate SOL (opz.)",  min_value=0.0, value=0.0, step=0.001, format="%.4f")
        fees_usdc = st.number_input("Fees accumulate USDC (opz.)", min_value=0.0, value=0.0, step=0.01,  format="%.2f")

    st.divider()
    st.subheader("4. Nuovo range proposto")
    scenario  = st.radio("Scenario", ["A — estendi solo il minimo", "B — estendi min + max"])
    p_min_new = st.number_input("Nuovo prezzo min ($)", min_value=1.0, value=float(round(p_curr)), step=1.0, format="%.2f")
    if "B" in scenario:
        p_max_new = st.number_input("Nuovo prezzo max ($)", min_value=p_min_new + 1.0, value=round(p_max_orig * 1.3, 0), step=1.0, format="%.2f")
    else:
        p_max_new = p_max_orig


# ─────────────────────────────────────────────────────────────────────────────
# VALIDAZIONI
# ─────────────────────────────────────────────────────────────────────────────

errors = []
if p_min_orig >= p_max_orig:
    errors.append("Il prezzo min originale deve essere < del max originale.")
if p_min_new >= p_max_new:
    errors.append("Il nuovo prezzo min deve essere < del nuovo max.")
if input_mode == "📍 Token attuali da Orca/Raydium" and sol_now == 0 and usdc_now == 0:
    errors.append("Inserisci almeno SOL o USDC nel pool.")

for e in errors:
    st.error(f"⛔ {e}")
if errors:
    st.stop()


# ─────────────────────────────────────────────────────────────────────────────
# CALCOLI PRINCIPALI
# ─────────────────────────────────────────────────────────────────────────────

if input_mode == "📍 Token attuali da Orca/Raydium":
    L_orig   = calc_L_from_tokens(sol_now, usdc_now, p_curr, p_min_orig, p_max_orig)
    val_pool = sol_now * p_curr + usdc_now
    val_fees = fees_sol * p_curr + fees_usdc
    val_now  = val_pool + val_fees
    # per crosscheck: token teorici da L
    sol_check, usdc_check = calc_tokens_from_L(L_orig, p_curr, p_min_orig, p_max_orig)
else:
    L_orig       = calc_L_from_capital_at_open(capital, p_open, p_min_orig, p_max_orig)
    sol_now, usdc_now = calc_tokens_from_L(L_orig, p_curr, p_min_orig, p_max_orig)
    val_pool     = sol_now * p_curr + usdc_now
    val_fees     = fees_sol * p_curr + fees_usdc
    val_now      = val_pool + val_fees
    sol_check, usdc_check = sol_now, usdc_now

# L nuovo range (reinvesti val_pool nel nuovo range, fees non entrano nel pool)
L_new = calc_L_new_from_value(val_pool, p_curr, p_min_new, p_max_new)

# Proiezioni
v_orig_now         = position_value(L_orig, p_curr,     p_min_orig, p_max_orig)
v_orig_at_orig_max = position_value(L_orig, p_max_orig, p_min_orig, p_max_orig)
v_new_at_orig_max  = position_value(L_new,  p_max_orig, p_min_new,  p_max_new)
v_new_at_new_max   = position_value(L_new,  p_max_new,  p_min_new,  p_max_new)

diff_at_max = v_new_at_orig_max - v_orig_at_orig_max
pct_diff    = diff_at_max / v_orig_at_orig_max * 100 if v_orig_at_orig_max > 0 else 0
l_ratio     = L_new / L_orig if L_orig > 0 else 0
fuori_range = p_curr < p_min_orig


# ─────────────────────────────────────────────────────────────────────────────
# STATO ATTUALE
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("📌 Stato attuale della posizione")

c1, c2, c3, c4, c5 = st.columns(5)
with c1:
    stato = "🔴 Fuori range (sotto)" if p_curr < p_min_orig else ("🟢 In range" if p_curr <= p_max_orig else "🔴 Fuori range (sopra)")
    st.metric("Stato", stato)
with c2:
    st.metric("SOL nel pool", f"{sol_now:.4f}", delta=f"≈ ${sol_now*p_curr:,.0f}")
with c3:
    st.metric("USDC nel pool", f"${usdc_now:,.2f}")
with c4:
    st.metric("Fees accumulate", f"${val_fees:,.2f}", delta=f"{fees_sol:.4f} SOL + ${fees_usdc:.2f}")
with c5:
    st.metric("Valore totale", f"${val_now:,.2f}", delta=f"solo pool: ${val_pool:,.2f}")

st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# SCENARI A CONFRONTO
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("🔀 Scenari a confronto")

c1, c2, c3 = st.columns(3)
with c1:
    st.metric(
        f"Originale → ${p_max_orig:.0f}",
        f"${v_orig_at_orig_max:,.0f}",
        delta=f"{v_orig_at_orig_max - val_pool:+,.0f}$ vs oggi"
    )
with c2:
    color_delta = f"{diff_at_max:+,.0f}$ vs originale ({pct_diff:+.1f}%)"
    st.metric(
        f"Nuovo range → ${p_max_orig:.0f}",
        f"${v_new_at_orig_max:,.0f}",
        delta=color_delta
    )
with c3:
    if "B" in scenario:
        st.metric(
            f"Nuovo range → ${p_max_new:.0f}",
            f"${v_new_at_new_max:,.0f}",
            delta=f"{v_new_at_new_max - val_pool:+,.0f}$ vs oggi"
        )
    else:
        st.metric(
            "Riduzione liquidità L",
            f"{(1-l_ratio)*100:.1f}%",
            delta="range più largo = L più diluita"
        )


# ─────────────────────────────────────────────────────────────────────────────
# GRAFICO
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("📈 Valore posizione al variare del prezzo SOL")

p_lo   = min(p_curr, p_min_new, p_min_orig) * 0.8
p_hi   = max(p_max_orig, p_max_new) * 1.12
prices = np.linspace(p_lo, p_hi, 500)

v_orig_line = [position_value(L_orig, p, p_min_orig, p_max_orig) for p in prices]
v_new_line  = [position_value(L_new,  p, p_min_new,  p_max_new)  for p in prices]
v_hold_line = [val_pool * p / p_curr for p in prices]  # hold con valore pool attuale

fig = go.Figure()
fig.add_trace(go.Scatter(x=prices, y=v_orig_line, name="Pool originale", mode="lines",
    line=dict(color="#4f9cf9", width=2.5),
    hovertemplate="SOL $%{x:.1f} → $%{y:,.2f}<extra>Pool originale</extra>"))
fig.add_trace(go.Scatter(x=prices, y=v_new_line,  name=f"Nuovo range ({scenario[0]})", mode="lines",
    line=dict(color="#34d399", width=2.5),
    hovertemplate="SOL $%{x:.1f} → $%{y:,.2f}<extra>Nuovo range</extra>"))
fig.add_trace(go.Scatter(x=prices, y=v_hold_line, name="Hold SOL puro", mode="lines",
    line=dict(color="#fb923c", width=1.5, dash="dot"),
    hovertemplate="SOL $%{x:.1f} → $%{y:,.2f}<extra>Hold SOL</extra>"))

fig.add_hline(y=val_pool, line_dash="dash", line_color="#a78bfa", line_width=1,
              annotation_text=f"Pool oggi ${val_pool:,.0f}", annotation_position="bottom right")

for xval, label, color in [
    (p_curr,     f"Ora ${p_curr:.0f}",           "#a78bfa"),
    (p_min_orig, f"Min orig ${p_min_orig:.0f}",   "#4f9cf9"),
    (p_max_orig, f"Max orig ${p_max_orig:.0f}",   "#4f9cf9"),
    (p_min_new,  f"Nuovo min ${p_min_new:.0f}",   "#34d399"),
]:
    fig.add_vline(x=xval, line_dash="dash", line_color=color, line_width=1, opacity=0.5,
                  annotation_text=label, annotation_font_size=10, annotation_font_color=color)
if "B" in scenario:
    fig.add_vline(x=p_max_new, line_dash="dash", line_color="#34d399", line_width=1, opacity=0.5,
                  annotation_text=f"Nuovo max ${p_max_new:.0f}", annotation_font_size=10, annotation_font_color="#34d399")

fig.update_layout(
    xaxis_title="Prezzo SOL ($)", yaxis_title="Valore posizione ($)",
    legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
    hovermode="x unified",
    plot_bgcolor="#12151c", paper_bgcolor="#0a0c10", font=dict(color="#e8eaf0"),
    xaxis=dict(gridcolor="rgba(255,255,255,0.05)"),
    yaxis=dict(gridcolor="rgba(255,255,255,0.05)", tickprefix="$"),
    height=460,
)
st.plotly_chart(fig, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# TABELLA DETTAGLIO TOKEN + VALORI
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("📋 Dettaglio composizione token ai prezzi chiave")

checkpoints = sorted(set([
    round(p_curr, 1), round(p_min_orig, 1),
    round((p_min_orig + p_max_orig) / 2, 1),
    round(p_max_orig, 1), round(p_min_new, 1),
    round(p_max_new, 1), round(p_max_orig * 1.2, 1),
]))

rows = []
for p in checkpoints:
    sol_o,  usd_o = calc_tokens_from_L(L_orig, p, p_min_orig, p_max_orig)
    sol_n,  usd_n = calc_tokens_from_L(L_new,  p, p_min_new,  p_max_new)
    val_o = sol_o * p + usd_o
    val_n = sol_n * p + usd_n
    rows.append({
        "Prezzo SOL": f"${p:,.1f}",
        "SOL (orig)": f"{sol_o:.4f}",
        "USDC (orig)": f"${usd_o:,.2f}",
        "Valore orig": f"${val_o:,.2f}",
        "SOL (nuovo)": f"{sol_n:.4f}",
        "USDC (nuovo)": f"${usd_n:,.2f}",
        "Valore nuovo": f"${val_n:,.2f}",
        "Δ valore": f"{val_n - val_o:+,.2f}$",
    })

st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# CONSIGLIO
# ─────────────────────────────────────────────────────────────────────────────

st.subheader("💡 Consiglio")

if not fuori_range:
    st.success("✅ La posizione è in range e sta generando fees. Non serve modificare nulla.")
else:
    loss_l_pct = (1 - l_ratio) * 100
    if "A" in scenario:
        st.warning(
            f"**Scenario A — estendi solo il minimo a ${p_min_new:.0f}:**  \n"
            f"La liquidità L si riduce del **{loss_l_pct:.1f}%**.  \n"
            f"Arrivi a **${v_new_at_orig_max:,.0f}** (invece di **${v_orig_at_orig_max:,.0f}**) "
            f"quando SOL raggiunge ${p_max_orig:.0f} — lasci {abs(diff_at_max):,.0f}$ sul tavolo.  \n"
            f"Pro: inizi a guadagnare fees immediatamente."
        )
    else:
        st.info(
            f"**Scenario B — estendi min a ${p_min_new:.0f} e max a ${p_max_new:.0f}:**  \n"
            f"L si riduce del **{loss_l_pct:.1f}%** ma puoi recuperare fino a **${v_new_at_new_max:,.0f}** "
            f"se SOL arriva a ${p_max_new:.0f}.  \n"
            f"Conviene se sei bullish a lungo termine."
        )

    st.markdown(f"""
---
**Opzione ottimale matematicamente:**

> **Chiudi la posizione** (realizzi **${val_pool:,.2f}**) e riaprila con range centrato attorno a ${p_curr:.0f}.  
> La impermanent loss è già avvenuta — aspettare non la recupera se SOL non torna nel range originale.  
> Riaprire centrato massimizza L e quindi le **fees future**.
""")

st.divider()
st.caption("Matematica: Uniswap v3 whitepaper Eq. 6.29/6.30 | L calcolata dai token reali | Non è consulenza finanziaria.")
