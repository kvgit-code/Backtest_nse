"""
Builds site/data.json: daily OHLC for the top-N NSE stocks by turnover.

Stock list
  1. If tickers.csv exists (column "symbol"), it is used as-is, in that order
     (order = entry priority in the dashboard).
  2. Else, if universe.json exists and REFRESH_LIST is not "true", that saved
     list is reused (only prices are updated).
  3. Else the Nifty 500 list is pulled from NSE, ranked by average daily
     turnover (close x volume) over the last TURNOVER_DAYS sessions, the top
     TOP_N are kept, and universe.json is rewritten. Rank 1 = highest priority.

Prices come from Yahoo Finance (SYMBOL.NS), split/dividend adjusted.

Env vars: YEARS=5  TOP_N=250  TURNOVER_DAYS=60  OUT=site/data.json  REFRESH_LIST=false
"""
import datetime as dt
import io
import json
import os
import sys
import time

import pandas as pd
import requests
import yfinance as yf

YEARS = int(os.getenv("YEARS", "5"))
TOP_N = int(os.getenv("TOP_N", "250"))
TURNOVER_DAYS = int(os.getenv("TURNOVER_DAYS", "60"))
OUT = os.getenv("OUT", "site/data.json")
UNIVERSE_FILE = os.getenv("UNIVERSE_FILE", "universe.json")
REFRESH_LIST = os.getenv("REFRESH_LIST", "false").strip().lower() == "true"
BATCH = 40

NSE_URLS = [
    "https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
    "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/csv,*/*",
}


def nse_symbols():
    sess = requests.Session()
    sess.headers.update(HEADERS)
    for url in NSE_URLS:
        try:
            r = sess.get(url, timeout=30)
            r.raise_for_status()
            syms = pd.read_csv(io.StringIO(r.text))["Symbol"].astype(str).str.strip().tolist()
            print(f"NSE list: {len(syms)} symbols from {url}")
            return syms
        except Exception as e:  # noqa: BLE001
            print(f"NSE list failed ({url}): {e}")
    return None


def get_universe():
    """Returns (symbols, rank_it, built_date, label)."""
    if os.path.exists("tickers.csv"):
        syms = pd.read_csv("tickers.csv")["symbol"].astype(str).str.strip().tolist()
        print(f"Using tickers.csv ({len(syms)} symbols, file order = priority)")
        return syms, False, None, "tickers.csv order"
    if not REFRESH_LIST and os.path.exists(UNIVERSE_FILE):
        with open(UNIVERSE_FILE) as f:
            u = json.load(f)
        print(f"Reusing saved list from {u.get('built')} ({len(u['symbols'])} symbols)")
        return u["symbols"], False, u.get("built"), u.get("ranked_by", "saved list")
    syms = nse_symbols()
    if not syms:
        sys.exit("Could not fetch the NSE list. Add a tickers.csv (column: symbol) and retry.")
    return syms, True, dt.date.today().isoformat(), f"{TURNOVER_DAYS}-day avg turnover"


def download(symbols):
    frames = {}
    for i in range(0, len(symbols), BATCH):
        batch = symbols[i:i + BATCH]
        tick = [s + ".NS" for s in batch]
        raw = None
        for attempt in range(3):
            try:
                raw = yf.download(tick, period=f"{YEARS}y", interval="1d", auto_adjust=True,
                                  group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:  # noqa: BLE001
                print(f"batch {i} attempt {attempt + 1} failed: {e}")
                time.sleep(5)
        if raw is None or raw.empty:
            continue
        for s, t in zip(batch, tick):
            try:
                df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
                df = df[["Open", "High", "Low", "Close", "Volume"]].dropna(subset=["Close", "High", "Low", "Open"])
            except KeyError:
                continue
            if len(df) >= 30:
                frames[s] = df
        print(f"downloaded {min(i + BATCH, len(symbols))}/{len(symbols)}")
    return frames


def main():
    symbols, rank_it, built, label = get_universe()
    frames = download(symbols)
    if not frames:
        sys.exit("No price data downloaded.")

    if rank_it:
        turnover = {s: float((df["Close"] * df["Volume"]).tail(TURNOVER_DAYS).mean()) for s, df in frames.items()}
        order = sorted(turnover, key=turnover.get, reverse=True)[:TOP_N]
        with open(UNIVERSE_FILE, "w") as f:
            json.dump({"built": built, "ranked_by": label, "symbols": order}, f)
        print(f"Saved new stock list to {UNIVERSE_FILE}")
    else:
        order = [s for s in symbols if s in frames]

    dates = sorted(set().union(*[frames[s].index for s in order]))
    idx = pd.DatetimeIndex(dates)

    def col(df, c):
        ser = df[c].reindex(idx)
        return [None if pd.isna(v) else round(float(v), 2) for v in ser.tolist()]

    out = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "source": "Yahoo Finance (adjusted)",
        "ranked_by": label,
        "list_built": built,
        "dates": [d.strftime("%Y-%m-%d") for d in idx],
        "symbols": [
            {"s": s, "o": col(frames[s], "Open"), "h": col(frames[s], "High"),
             "l": col(frames[s], "Low"), "c": col(frames[s], "Close")}
            for s in order
        ],
    }
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print(f"Wrote {OUT}: {len(order)} symbols, {len(idx)} days, {os.path.getsize(OUT) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
