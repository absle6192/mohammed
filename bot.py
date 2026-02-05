import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta

import pytz

from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest


# =========================
# ENV helpers
# =========================
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def env_any(names: list[str], default: str | None = None) -> str:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    if default is None:
        raise RuntimeError(f"Missing env var (any of): {', '.join(names)}")
    return str(default).strip()


def env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        raise RuntimeError(f"Invalid float for env var: {name}")


def env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        raise RuntimeError(f"Invalid int for env var: {name}")


def env_bool(name: str, default: str = "false") -> bool:
    v = os.getenv(name, default)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fmt_time_sa(dt_utc: datetime) -> str:
    # Saudi Arabia timezone
    sa = pytz.timezone("Asia/Riyadh")
    return dt_utc.astimezone(sa).strftime("%Y-%m-%d %H:%M:%S")


# =========================
# Telegram
# =========================
def send_telegram(text: str) -> None:
    token = env_any(["TELEGRAM_BOT_TOKEN", "TELEGRAM_TOKEN"])
    chat_id = env_any(["TELEGRAM_CHAT_ID", "TELEGRAM_CHATID", "TELEGRAM_ID"])
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=15)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text}")


# =========================
# Symbols / config
# =========================
def parse_symbols() -> list[str]:
    raw = os.getenv("SYMBOLS") or os.getenv("TICKERS") or ""
    raw = raw.strip()
    if not raw:
        # fallback
        return ["TSLA", "AAPL", "NVDA", "AMD", "AMZN", "GOOGL", "MU", "MSFT"]
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]


# =========================
# Alpaca clients (0.27 compatible)
# =========================
def build_clients() -> tuple[StockHistoricalDataClient, TradingClient, bool]:
    api_key = env_any(["ALPACA_API_KEY", "APCA_API_KEY_ID", "APCA_API_KEY"])
    secret = env_any(["ALPACA_API_SECRET", "APCA_API_SECRET_KEY", "APCA_API_SECRET"])

    paper = env_bool("ALPACA_PAPER", "true")
    # IMPORTANT: alpaca-py 0.27.0 TradingClient does NOT accept base_url kwarg here
    trade = TradingClient(api_key, secret, paper=paper)

    # IMPORTANT: StockHistoricalDataClient does NOT accept feed kwarg in 0.27.0
    hist = StockHistoricalDataClient(api_key, secret)

    return hist, trade, paper


# =========================
# Candle filter (LIGHT)
# =========================
def candle_filter_light(open_p: float, high_p: float, low_p: float, close_p: float) -> tuple[bool, str]:
    # Light filter: avoid long upper wicks & tiny bodies
    body = abs(close_p - open_p)
    rng = max(high_p - low_p, 1e-9)
    upper_wick = high_p - max(open_p, close_p)
    lower_wick = min(open_p, close_p) - low_p

    body_ratio = body / rng
    upper_ratio = upper_wick / rng

    # PASS if body is reasonable and upper wick not huge
    if body_ratio >= 0.35 and upper_ratio <= 0.45:
        return True, "PASS"
    return False, "FAIL"


# =========================
# Core calculations
# =========================
def get_bars(hist: StockHistoricalDataClient, symbol: str, minutes: int, feed: DataFeed) -> list:
    end = now_utc()
    start = end - timedelta(minutes=minutes)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=feed,  # ✅ put feed here to avoid SIP permission error
    )
    resp = hist.get_stock_bars(req)
    df = resp.df
    if df is None or len(df) == 0:
        return []
    # df is multiindex when multiple symbols; for single symbol it still may be multi
    try:
        sdf = df.xs(symbol)
    except Exception:
        sdf = df
    return list(sdf.itertuples())


def mean(vals: list[float]) -> float:
    if not vals:
        return float("nan")
    return sum(vals) / len(vals)


def safe_pct(a: float, b: float) -> float:
    # (a - b) / b
    if b == 0:
        return 0.0
    return (a - b) / b


# =========================
# Trading (optional)
# =========================
def submit_market_order(trade: TradingClient, symbol: str, side: OrderSide, usd: float) -> str:
    # Estimate qty from last trade price is not available here; we keep it simple:
    # Use notional orders are not supported in all contexts; we use qty = floor(usd / price)
    # We'll pass qty later after we compute last_close.
    raise NotImplementedError


# =========================
# Main loop
# =========================
def main() -> None:
    symbols = parse_symbols()

    # Mode:
    # ALERTS = only telegram alerts
    # TRADE  = can trade if AUTO_TRADE=on
    mode = (os.getenv("MODE") or "ALERTS").strip().upper()
    auto_trade = env_bool("AUTO_TRADE", "false")

    interval_sec = env_int("INTERVAL_SEC", os.getenv("INTERVAL", "15"))

    # thresholds
    ma_min = env_int("MA_MIN", os.getenv("MA_WINDOW_MIN", "3"))          # MA(3m)
    baseline_min = env_int("BASELINE_MIN", os.getenv("VOL_BASELINE_MIN", "30"))
    min_diff_pct = env_float("MIN_DIFF_PCT", os.getenv("MIN_DIFF", "0.0010"))   # 0.10%
    max_diff_pct = env_float("MAX_DIFF_PCT", os.getenv("MAX_DIFF", "0.0030"))   # 0.30%
    min_vol_ratio = env_float("MIN_VOL_RATIO", os.getenv("MIN_VOL", "1.4"))     # e.g. 1.4x
    recent_window_min = env_int("RECENT_WINDOW_MIN", "10")
    max_recent_move_pct = env_float("MAX_RECENT_MOVE_PCT", "0.003")      # 0.30%
    candle_filter = (os.getenv("CANDLE_FILTER") or "LIGHT").strip().upper()

    # data feed: IEX for free accounts (avoid SIP errors)
    data_feed = (os.getenv("DATA_FEED") or "IEX").strip().upper()
    feed = DataFeed.IEX if data_feed == "IEX" else DataFeed.SIP

    hist, trade, paper = build_clients()

    # Start message (IMPORTANT: no trading.paper here)
    try:
        send_telegram(
            f"✅ Bot started ({mode}) | symbols={','.join(symbols)} | "
            f"interval={interval_sec}s | feed={data_feed} | paper={paper} | candle={candle_filter} | auto_trade={auto_trade}"
        )
    except Exception as e:
        # If telegram fails, still keep running to show logs
        print(f"Telegram start failed: {e}")

    last_alert_at: dict[str, float] = {}
    min_alert_gap_sec = 20.0  # basic spam control

    while True:
        for sym in symbols:
            try:
                # Pull enough bars for baseline + MA + recent window
                need_min = max(baseline_min, ma_min, recent_window_min) + 2
                bars = get_bars(hist, sym, minutes=need_min, feed=feed)
                if len(bars) < max(ma_min, 3):
                    continue

                # Latest bar
                last = bars[-1]
                close_p = float(last.close)
                open_p = float(last.open)
                high_p = float(last.high)
                low_p = float(last.low)
                vol = float(last.volume)

                # MA over last ma_min bars (use closes)
                ma_closes = [float(b.close) for b in bars[-ma_min:]]
                ma = mean(ma_closes)
                diff_pct = safe_pct(close_p, ma)  # (close - ma)/ma

                # volume baseline average over baseline_min bars (exclude latest to be safe)
                base_slice = bars[-(baseline_min + 1):-1] if len(bars) >= baseline_min + 1 else bars[:-1]
                base_vols = [float(b.volume) for b in base_slice if float(b.volume) > 0]
                vol_avg = mean(base_vols) if base_vols else float("nan")
                vol_ratio = (vol / vol_avg) if (vol_avg and not math.isnan(vol_avg) and vol_avg > 0) else 0.0

                # recent move over recent_window_min (compare now vs N minutes ago)
                idx_back = max(1, recent_window_min)
                if len(bars) <= idx_back:
                    recent_move = 0.0
                else:
                    prev_close = float(bars[-(idx_back + 1)].close)
                    recent_move = safe_pct(close_p, prev_close)

                # Candle filter (LIGHT)
                candle_pass = True
                candle_status = "PASS"
                if candle_filter == "LIGHT":
                    candle_pass, candle_status = candle_filter_light(open_p, high_p, low_p, close_p)

                # Signal logic
                # We alert when:
                # 1) abs(diff) between min/max
                # 2) vol spike >= min_vol_ratio
                # 3) recent move not too large (avoid chasing)
                diff_abs = abs(diff_pct)
                if not (min_diff_pct <= diff_abs <= max_diff_pct):
                    continue
                if vol_ratio < min_vol_ratio:
                    continue
                if abs(recent_move) > max_recent_move_pct:
                    continue
                if not candle_pass:
                    continue

                # Rate limit per symbol
                t = time.time()
                if sym in last_alert_at and (t - last_alert_at[sym]) < min_alert_gap_sec:
                    continue
                last_alert_at[sym] = t

                direction = "LONG" if diff_pct > 0 else "SHORT"
                arrow = "⬆️" if direction == "LONG" else "⬇️"

                msg = (
                    f"📣 Signal: {direction} | {sym}\n"
                    f"Price: {close_p:.2f} | السعر 💰\n"
                    f"MA({ma_min}m): {ma:.2f} | المتوسط ({ma_min}د) 📊\n"
                    f"Diff: {diff_pct*100:+.2f}% | الفرق {arrow}\n\n"
                    f"Volume Spike (baseline):\n"
                    f"(x{vol_ratio:.2f}) {int(vol)} مقابل {int(vol_avg) if not math.isnan(vol_avg) else 0} 🔥\n\n"
                    f"Recent Move | حركة {recent_window_min}د الأخيرة 🧠:\n"
                    f"{recent_move*100:+.2f}%\n\n"
                    f"Candle Filter (LIGHT):\n"
                    f"{'✅' if candle_status=='PASS' else '❌'} {candle_status}\n\n"
                    f"Strength | قوة الإشارة ⭐:\n"
                    f"{'✅ متوسطة (OK)'}\n\n"
                    f"Time (UTC) | الوقت ⏱:\n"
                    f"{now_utc().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                send_telegram(msg)

                # Trading (optional) - OFF unless MODE=TRADE and AUTO_TRADE=on
                if mode == "TRADE" and auto_trade:
                    usd_per_trade = env_float("USD_PER_TRADE", "2000")
                    # qty by last price
                    qty = max(1, int(usd_per_trade // close_p))
                    side = OrderSide.BUY if direction == "LONG" else OrderSide.SELL

                    order = MarketOrderRequest(
                        symbol=sym,
                        qty=qty,
                        side=side,
                        time_in_force=TimeInForce.DAY,
                    )
                    o = trade.submit_order(order)
                    send_telegram(f"✅ Order sent: {sym} {side.value} qty={qty} | paper={paper}")

            except APIError as e:
                # Alpaca API errors (including SIP permissions)
                try:
                    send_telegram(f"⚠️ Bot error: APIError:\n{str(e)}")
                except Exception:
                    pass
            except Exception as e:
                try:
                    send_telegram(f"⚠️ Bot error:\n{repr(e)}")
                except Exception:
                    pass

        time.sleep(max(5, interval_sec))


if __name__ == "__main__":
    main()
