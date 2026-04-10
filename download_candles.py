"""
Scarica / aggiorna candele SOL/USDT 15m da Binance (API pubblica, no account richiesto)
e le salva nel formato: timestamp,open,high,low,close,volume

Uso:
    # Aggiorna il CSV esistente con le candele mancanti fino ad ora (default)
    python download_candles.py

    # Scarica un anno intero (sovrascrive il CSV)
    python download_candles.py --full

    # Range personalizzato
    python download_candles.py --start 2025-01-01 --end 2025-06-01

Dipendenze:
    pip install requests pandas
"""

import argparse
import os
import requests
import pandas as pd
import time
from datetime import datetime, timezone

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURAZIONE
# ─────────────────────────────────────────────────────────────────────────────

SYMBOL      = "SOLUSDT"
INTERVAL    = "15m"
INTERVAL_MS = 15 * 60 * 1000   # durata di una candela in millisecondi
LIMIT       = 1000              # max candele per chiamata Binance
OUTPUT_FILE = "SOL_USDT_15m.csv"
PAUSE_SEC   = 0.3               # pausa tra le chiamate per non essere bannati

BASE_URL = "https://api.binance.com/api/v3/klines"

# ─────────────────────────────────────────────────────────────────────────────
# DOWNLOAD
# ─────────────────────────────────────────────────────────────────────────────

def fetch_klines(symbol, interval, start_ms, end_ms, limit=1000):
    """Scarica tutte le candele nel range con loop automatico."""
    all_candles = []
    current_start = start_ms
    total_expected = (end_ms - start_ms) // INTERVAL_MS

    print(f"Download {symbol} {interval}")
    print(f"Da:  {datetime.fromtimestamp(start_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"A:   {datetime.fromtimestamp(end_ms/1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC")
    print(f"Candele attese: ~{total_expected:,}")
    print("-" * 50)

    batch = 0
    while current_start < end_ms:
        params = {
            "symbol":    symbol,
            "interval":  interval,
            "startTime": current_start,
            "endTime":   end_ms,
            "limit":     limit,
        }

        try:
            resp = requests.get(BASE_URL, params=params, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.RequestException as e:
            print(f"  Errore di rete: {e} — riprovo tra 5 secondi...")
            time.sleep(5)
            continue

        if not data:
            break

        all_candles.extend(data)
        batch += 1

        last_ts     = data[-1][0]
        last_dt_str = datetime.fromtimestamp(last_ts/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
        pct         = min(100, (last_ts - start_ms) / max(end_ms - start_ms, 1) * 100)
        print(f"  Batch {batch:3d} | fino a {last_dt_str} | {len(all_candles):,} candele | {pct:.1f}%")

        current_start = last_ts + INTERVAL_MS
        time.sleep(PAUSE_SEC)

    return all_candles


def parse_candles(raw):
    """
    Converte la risposta Binance nel formato:
    timestamp,open,high,low,close,volume
    """
    rows = []
    for c in raw:
        ts    = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        open_ = float(c[1])
        high  = float(c[2])
        low   = float(c[3])
        close = float(c[4])
        vol   = round(float(c[5]), 2)
        rows.append([ts, open_, high, low, close, vol])
    return rows


def save_csv(rows, filepath, existing_df=None):
    """Salva (o aggiorna) il CSV, evitando duplicati e mantenendo l'ordinamento."""
    new_df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])

    if existing_df is not None and not existing_df.empty:
        combined = pd.concat([existing_df, new_df], ignore_index=True)
    else:
        combined = new_df

    combined = (
        combined
        .drop_duplicates(subset="timestamp")
        .sort_values("timestamp")
        .reset_index(drop=True)
    )
    combined.to_csv(filepath, index=False)
    return combined


def load_existing(filepath):
    """Carica il CSV esistente; restituisce un DataFrame vuoto se non esiste."""
    if os.path.exists(filepath):
        df = pd.read_csv(filepath)
        return df
    return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])


def last_timestamp_ms(df):
    """Restituisce il timestamp (ms) dell'ultima candela nel DataFrame."""
    if df.empty:
        return None
    last_str = df["timestamp"].iloc[-1]
    dt = datetime.strptime(last_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)

# ─────────────────────────────────────────────────────────────────────────────
# ARGOMENTI
# ─────────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="Scarica/aggiorna candele SOL/USDT 15m da Binance"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Scarica l'ultimo anno completo (sovrascrive il CSV esistente)",
    )
    parser.add_argument(
        "--start",
        type=str,
        default=None,
        help="Data inizio (YYYY-MM-DD). Ignorato in modalità update automatica.",
    )
    parser.add_argument(
        "--end",
        type=str,
        default=None,
        help="Data fine (YYYY-MM-DD). Default: adesso.",
    )
    return parser.parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    args = parse_args()

    print("=" * 50)
    print("  Binance Candle Downloader — SOL/USDT 15m")
    print("=" * 50)

    now_utc = datetime.now(timezone.utc)
    end_ms  = int(now_utc.timestamp() * 1000) if args.end is None else \
              int(datetime.strptime(args.end, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp() * 1000)

    existing_df = load_existing(OUTPUT_FILE)

    # ── Determina start_ms ──────────────────────────────────────────────────
    if args.full:
        # Modalità full: scarica l'ultimo anno (sovrascrive)
        start_dt = now_utc.replace(year=now_utc.year - 1)
        start_ms = int(start_dt.timestamp() * 1000)
        existing_df = pd.DataFrame(columns=existing_df.columns)
        print("Modalità: FULL (ultimo anno)")

    elif args.start is not None:
        # Range manuale
        start_ms = int(
            datetime.strptime(args.start, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp() * 1000
        )
        print(f"Modalità: range manuale dal {args.start}")

    else:
        # Modalità UPDATE automatica (default — nessun parametro)
        last_ms = last_timestamp_ms(existing_df)
        if last_ms is None:
            # Nessun file: scarica l'ultimo anno
            start_dt = now_utc.replace(year=now_utc.year - 1)
            start_ms = int(start_dt.timestamp() * 1000)
            print("Nessun CSV trovato — scarico l'ultimo anno.")
        else:
            # Riprendi dalla candela successiva all'ultima già salvata
            start_ms = last_ms + INTERVAL_MS
            last_str = datetime.fromtimestamp(last_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")
            print(f"Modalità: UPDATE — ultima candela presente: {last_str} UTC")

    if start_ms >= end_ms:
        print("\nIl CSV e' gia' aggiornato. Nessuna candela mancante.")
        exit(0)

    # ── Download ────────────────────────────────────────────────────────────
    raw = fetch_klines(SYMBOL, INTERVAL, start_ms, end_ms, LIMIT)

    if not raw:
        print("Nessun nuovo dato ricevuto.")
        exit(0)

    print(f"\nParsing {len(raw):,} candele...")
    rows = parse_candles(raw)

    print(f"Salvataggio in {OUTPUT_FILE}...")
    df = save_csv(rows, OUTPUT_FILE, existing_df=existing_df)

    print("\n" + "=" * 50)
    print(f"  Completato!")
    print(f"  Nuove candele scaricate : {len(raw):,}")
    print(f"  Candele totali nel file : {len(df):,}")
    print(f"  Prima candela           : {df['timestamp'].iloc[0]}")
    print(f"  Ultima candela          : {df['timestamp'].iloc[-1]}")
    print(f"  File salvato            : {OUTPUT_FILE}")
    print("=" * 50)

    print("\nAnteprima ultime 3 righe:")
    print(df.tail(3).to_string(index=False))
