# bot.py
import os
import time
import math
from datetime import datetime, timezone
from typing import Dict, List, Tuple, Optional

import requests

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.common.exceptions import APIError


# =========================
# ENV / SETTINGS
# =========================
APCA_API_KEY_ID = os.getenv("APCA_API_KEY_ID", "").strip()
APCA_API_SECRET_KEY = os.getenv("APCA_API_SECRET_KEY", "").strip()
APCA_API_BASE_URL = os.getenv("APCA_API_BASE_URL", "https://paper-api.alpaca.markets").strip()  # for reference

# Symbols to watch (8 by default)
SYMBOLS = [s.strip().upper() for s in os.getenv(
    "SYMBOLS",
    "TSLA,NVDA,AAPL,AMZN,AMD,GOOGL,MU,MSFT"
).split(",") if s.strip()]

# Loop
CHECK_EVERY_SEC = int(os.getenv("CHECK_EVERY_SEC", "15"))

# Momentum windows (minutes)
MOM1_MIN = int(os.getenv("MOM1_MIN", "1"))
MOM5_MIN = int(os.getenv("MOM5_MIN", "5"))
MOM15_MIN = int(os.getenv("MOM15_MIN", "15"))

# Scoring weights
W_MOM = float(os.getenv("W_MOM", "0.55"))        # momentum weight
W_VOL = float(os.getenv("W_VOL", "0.30"))        # volume spike weight
W_TREND = float(os.getenv("W_TREND", "0.15"))    # trend confirmation weight

# Thresholds
MOM_THRESHOLD_PCT = float(os.getenv("MOM_THRESHOLD_PCT", "0.18"))  # decision threshold in % (BUY/SHORT vs WAIT)
MIN_VOL_MULT = float(os.getenv("MIN_VOL_MULT", "1.20"))            # require some volume spike (>= 1.2x) to prefer entries
MAX_SPREAD_PCT = float(os.getenv("MAX_SPREAD_PCT", "0.20"))        # if spread% > 0.20% => skip (illiquid / bad fills)

# Alerts
SIGNAL_ONLY = os.getenv("SIGNAL_ONLY", "1").strip() == "1"         # 1 => never place orders (signals only)
SEND_ON_CHANGE_ONLY = os.getenv("SEND_ON_CHANGE_ONLY", "1").strip() == "1"
ALERT_MIN_INTERVAL_SEC = int(os.getenv("ALERT_MIN_INTERVAL_SEC", "60"))

# Telegram / Discord
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

# Internal state
_last_payload_hash = None
_last_sent_ts = 0


# =========================
# HELPERS
# =========================
def now_s() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

def pct_change(old: float, new: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / old * 100.0

def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))

def safe_float(x, default=0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default

def send_message(text: str) -> None:
    # Discord
    if DISCORD_WEBHOOK_URL:
        try:
            requests.post(DISCORD_WEBHOOK_URL, json={"content": text}, timeout=10)
        except Exception:
            pass

    # Telegram
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
            requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
        except Exception:
            pass

def should_alert(payload_key: str) -> bool:
    global _last_payload_hash, _last_sent_ts
    t = time.time()
    if t - _last_sent_ts < ALERT_MIN_INTERVAL_SEC:
        return False
    if SEND_ON_CHANGE_ONLY and payload_key == _last_payload_hash:
        return False
    _last_payload_hash = payload_key
    _last_sent_ts = t
    return True


# =========================
# DATA ACCESS
# =========================
def make_clients() -> Tuple[StockHistoricalDataClient, StockHistoricalDataClient]:
    # alpaca-py data client uses key/secret only; base_url is mainly for trading client, but ok.
    # We'll just use data client here.
    return (
        StockHistoricalDataClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY),
        StockHistoricalDataClient(APCA_API_KEY_ID, APCA_API_SECRET_KEY),
    )

def fetch_latest_quotes(data_client: StockHistoricalDataClient, symbols: List[str]) -> Dict[str, dict]:
    req = StockLatestQuoteRequest(symbol_or_symbols=symbols)
    quotes = data_client.get_stock_latest_quote(req)
    # quotes is dict-like keyed by symbol
    out = {}
    for s in symbols:
        if s in quotes:
            out[s] = quotes[s]
    return out

def fetch_bars(data_client: StockHistoricalDataClient, symbols: List[str], minutes: int) -> Dict[str, List[dict]]:
    # Fetch 1-min bars for the last N minutes (limit=minutes)
    req = StockBarsRequest(
        symbol_or_symbols=symbols,
        timeframe=TimeFrame.Minute,
        limit=minutes
    )
    bars = data_client.get_stock_bars(req)
    # bars.df exists but keep dict approach for clarity
    out: Dict[str, List[dict]] = {s: [] for s in symbols}
    for bar in bars:
        out[bar.symbol].append(bar)
    # Ensure chronological
    for s in symbols:
        out[s] = sorted(out[s], key=lambda b: b.timestamp)
    return out


# =========================
# SCORING / DECISION
# =========================
def spread_pct_from_quote(q) -> Optional[float]:
    # q has .bid_price / .ask_price
    bid = safe_float(getattr(q, "bid_price", 0.0))
    ask = safe_float(getattr(q, "ask_price", 0.0))
    if bid <= 0 or ask <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 100.0

def calc_momentum_from_bars(bars: List[dict]) -> Optional[Tuple[float, float]]:
    # returns (mom_pct, last_close)
    if not bars or len(bars) < 2:
        return None
    first = safe_float(getattr(bars[0], "close", 0.0))
    last = safe_float(getattr(bars[-1], "close", 0.0))
    if first <= 0 or last <= 0:
        return None
    return pct_change(first, last), last

def avg_volume(bars: List[dict]) -> float:
    vols = [safe_float(getattr(b, "volume", 0.0)) for b in bars if safe_float(getattr(b, "volume", 0.0)) > 0]
    if not vols:
        return 0.0
    return sum(vols) / len(vols)

def trend_confirm(mom1: float, mom5: float, mom15: float) -> float:
    # returns [0..1] confirmation score
    # same direction across timeframes => higher
    s1 = 1 if mom1 >= 0 else -1
    s5 = 1 if mom5 >= 0 else -1
    s15 = 1 if mom15 >= 0 else -1
    agree = (s1 == s5) + (s5 == s15) + (s1 == s15)  # 0..3
    return agree / 3.0

def score_symbol(mom1: float, mom5: float, mom15: float, vol_mult: float) -> float:
    # Normalize momentum roughly: cap at +/-1% for scoring range [-1..1]
    mom_score = clamp(mom5 / 1.0, -1.0, 1.0)  # 1% move => full scale
    # Volume spike: 1.0x => 0, 3.0x => 1 (cap)
    vol_score = clamp((vol_mult - 1.0) / 2.0, 0.0, 1.0)
    trend_score = trend_confirm(mom1, mom5, mom15)  # 0..1

    # Combine: momentum carries sign, other terms boost confidence
    base = W_MOM * mom_score
    boost = (W_VOL * vol_score) + (W_TREND * (trend_score - 0.5))  # center trend around 0
    return base * (1.0 + boost)

def decide(mom5: float, vol_mult: float, spread_pct: Optional[float]) -> Tuple[str, str]:
    # Decision + reason
    if spread_pct is None:
        return "WAIT", "No quote/spread data"
    if spread_pct > MAX_SPREAD_PCT:
        return "WAIT", f"Spread too wide ({spread_pct:.3f}%)"

    if abs(mom5) < MOM_THRESHOLD_PCT:
        return "WAIT", f"Momentum too small ({mom5:.3f}%)"

    if vol_mult < MIN_VOL_MULT:
        return "WAIT", f"Volume weak ({vol_mult:.2f}x)"

    return ("BUY", f"Mom +{mom5:.3f}% & Vol {vol_mult:.2f}x") if mom5 > 0 else ("SHORT", f"Mom {mom5:.3f}% & Vol {vol_mult:.2f}x")


# =========================
# MAIN LOOP
# =========================
def main():
    if not APCA_API_KEY_ID or not APCA_API_SECRET_KEY:
        raise SystemExit("Missing APCA_API_KEY_ID / APCA_API_SECRET_KEY in environment variables.")

    data_client, quote_client = make_clients()

    print(f"[{now_s()}] Bot started. Symbols={SYMBOLS} SIGNAL_ONLY={SIGNAL_ONLY}")
    print(f"[{now_s()}] Using BASE_URL(ref)={APCA_API_BASE_URL} (Paper should be https://paper-api.alpaca.markets)")

    # We need enough bars for the largest window
    max_window = max(MOM1_MIN, MOM5_MIN, MOM15_MIN)
    # For volume baseline, we’ll look at 30 minutes if possible
    vol_baseline_minutes = int(os.getenv("VOL_BASELINE_MIN", "30"))
    vol_baseline_minutes = max(vol_baseline_minutes, max_window)

    while True:
        try:
            # Fetch bars
            bars_mom1 = fetch_bars(data_client, SYMBOLS, MOM1_MIN + 1)
            bars_mom5 = fetch_bars(data_client, SYMBOLS, MOM5_MIN + 1)
            bars_mom15 = fetch_bars(data_client, SYMBOLS, MOM15_MIN + 1)
            bars_vol = fetch_bars(data_client, SYMBOLS, vol_baseline_minutes)

            # Fetch quotes (for spread)
            quotes = fetch_latest_quotes(quote_client, SYMBOLS)

            scored_rows = []
            for sym in SYMBOLS:
                q = quotes.get(sym)
                spread = spread_pct_from_quote(q) if q else None

                m1 = calc_momentum_from_bars(bars_mom1.get(sym, []))
                m5 = calc_momentum_from_bars(bars_mom5.get(sym, []))
                m15 = calc_momentum_from_bars(bars_mom15.get(sym, []))
                if not m1 or not m5 or not m15:
                    continue

                mom1, last_price = m1[0], m5[1]
                mom5 = m5[0]
                mom15 = m15[0]

                # Volume multiplier = avg volume last 5m / avg volume baseline
                vol5 = avg_volume(bars_mom5.get(sym, []))
                vol_base = avg_volume(bars_vol.get(sym, []))
                vol_mult = (vol5 / vol_base) if vol_base > 0 else 0.0

                dec, reason = decide(mom5, vol_mult, spread)
                score = score_symbol(mom1, mom5, mom15, vol_mult)

                # Skip illiquid by spread filter at ranking level too (optional)
                if spread is not None and spread > MAX_SPREAD_PCT:
                    # keep it but it will likely be WAIT; you can skip completely if you want
                    pass

                scored_rows.append({
                    "sym": sym,
                    "price": last_price,
                    "mom1": mom1,
                    "mom5": mom5,
                    "mom15": mom15,
                    "vol_mult": vol_mult,
                    "spread": spread,
                    "score": score,
                    "decision": dec,
                    "reason": reason
                })

            if not scored_rows:
                print(f"[{now_s()}] No data rows this cycle (bars/quotes missing).")
                time.sleep(CHECK_EVERY_SEC)
                continue

            # Rank by absolute score magnitude (strongest signal), but prefer non-WAIT
            # We'll sort: non-WAIT first, then abs(score) desc
            scored_rows.sort(key=lambda r: (r["decision"] == "WAIT", -abs(r["score"])))

            best = scored_rows[0]
            best_line = f"BEST={best['sym']} => {best['decision']} | mom5={best['mom5']:.3f}% vol={best['vol_mult']:.2f}x spread={(best['spread'] or 0):.3f}% price={best['price']:.2f} | {best['reason']}"

            # Print ranking (top 8)
            print(f"\n[{now_s()}] ===== RANKING =====")
            for i, r in enumerate(scored_rows[:len(SYMBOLS)], start=1):
                sp = r["spread"]
                sp_s = f"{sp:.3f}%" if sp is not None else "NA"
                print(
                    f"{i:02d}) {r['sym']:<5} {r['decision']:<5} "
                    f"score={r['score']:+.3f} mom1={r['mom1']:+.3f}% mom5={r['mom5']:+.3f}% mom15={r['mom15']:+.3f}% "
                    f"vol={r['vol_mult']:.2f}x spread={sp_s} price={r['price']:.2f}"
                )
            print(f"[{now_s()}] {best_line}")

            # Alert payload key
            payload_key = f"{best['sym']}|{best['decision']}|{round(best['mom5'],3)}|{round(best['vol_mult'],2)}"
            if should_alert(payload_key):
                # Short message (Telegram-friendly)
                msg_lines = [
                    f"📡 BEST SIGNAL",
                    f"{best['sym']} → {best['decision']}",
                    f"Price: {best['price']:.2f}",
                    f"Mom(1/5/15m): {best['mom1']:+.3f}% / {best['mom5']:+.3f}% / {best['mom15']:+.3f}%",
                    f"Vol spike: {best['vol_mult']:.2f}x",
                    f"Spread: {(best['spread'] or 0):.3f}%",
                    f"Reason: {best['reason']}",
                    "",
                    "Top 3:",
                ]
                for r in scored_rows[:3]:
                    msg_lines.append(
                        f"- {r['sym']} {r['decision']} | mom5={r['mom5']:+.3f}% vol={r['vol_mult']:.2f}x"
                    )
                send_message("\n".join(msg_lines))

            # NOTE: SIGNAL_ONLY mode doesn't place orders.
            # If later you want AUTO-TRADING, we add trading client + risk rules.

        except APIError as e:
            print(f"[{now_s()}] Alpaca APIError: {e}")
        except Exception as e:
            print(f"[{now_s()}] ERROR: {type(e).__name__}: {e}")

        time.sleep(CHECK_EVERY_SEC)


if __name__ == "__main__":
    main()
