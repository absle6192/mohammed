import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple, List

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest


# =========================
# Helpers
# =========================
def env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def env_any(names: List[str], default: Optional[str] = None) -> str:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    if default is not None:
        return default
    raise RuntimeError(f"Missing env var. Set ONE of: {', '.join(names)}")


def env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        raise RuntimeError(f"Invalid float for {name}")


def env_int(name: str, default: str) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except Exception:
        raise RuntimeError(f"Invalid int for {name}")


def env_bool(name: str, default: str = "false") -> bool:
    v = os.getenv(name, default)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_symbols() -> List[str]:
    raw = os.getenv("SYMBOLS") or os.getenv("TICKERS") or ""
    raw = raw.strip()
    if not raw:
        return ["TSLA", "AAPL", "NVDA", "AMD", "AMZN", "GOOGL", "MU", "MSFT"]
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]


def send_telegram(text: str) -> None:
    token = env_any(["TELEGRAM_BOT_TOKEN"])
    chat_id = env_any(["TELEGRAM_CHAT_ID"])
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=15)
    # لا نكسر البوت بسبب تليجرام
    if r.status_code != 200:
        print("Telegram error:", r.status_code, r.text)


# =========================
# Alpaca clients (FIXED)
# =========================
def build_clients() -> Tuple[StockHistoricalDataClient, TradingClient, bool]:
    """
    يقبل مفاتيح أي من النظامين:
    - الجديد: ALPACA_API_KEY / ALPACA_SECRET_KEY
    - القديم: APCA_API_KEY_ID / APCA_API_SECRET_KEY
    """
    api_key = env_any(["ALPACA_API_KEY", "APCA_API_KEY_ID"])
    secret = env_any(["ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY"])

    paper = env_bool("ALPACA_PAPER", "true")  # خليها true لحساب Paper

    # ✅ Data client: نستخدم IEX في الطلب نفسه (feed="iex")
    hist = StockHistoricalDataClient(api_key, secret)

    # ✅ Trading client: لا نمرر base_url نهائيًا (كان يسبب الخطأ عندك)
    trade = TradingClient(api_key, secret, paper=paper)

    return hist, trade, paper


# =========================
# Strategy config (Env)
# =========================
INTERVAL_SEC = env_int("INTERVAL_SEC", "15")

MA_MIN = env_int("MA_MIN", "3")  # متوسط 3 دقائق
MIN_DIFF_PCT = env_float("MIN_DIFF_PCT", "0.0010")  # 0.10%
MIN_VOL_RATIO = env_float("MIN_VOL_RATIO", "1.4")   # x1.4
RECENT_WINDOW_MIN = env_int("RECENT_WINDOW_MIN", "10")
MAX_RECENT_MOVE_PCT = env_float("MAX_RECENT_MOVE_PCT", "0.0030")  # 0.30%

# فلتر شموع خفيف
CANDLE_FILTER = os.getenv("CANDLE_FILTER", "LIGHT").strip().upper()  # LIGHT / OFF

# تداول؟ (افتراضي OFF)
AUTO_TRADING = env_bool("AUTO_TRADING", "false")

USD_PER_TRADE = env_float("USD_PER_TRADE", "2000")

# لو تبي فقط اشعارات دائمًا:
MODE = os.getenv("MODE", "ALERTS").strip().upper()  # ALERTS / TRADE


# =========================
# Candle filter (LIGHT)
# =========================
def candle_filter_light(o: float, h: float, l: float, c: float, side: str) -> bool:
    # حماية
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    body_ratio = body / rng

    # جسم واضح
    if body_ratio < 0.35:
        return False

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    upper_ratio = upper_wick / rng
    lower_ratio = lower_wick / rng

    if side == "LONG":
        # ما نبي ظل علوي طويل (رفض)
        if c <= o:
            return False
        if upper_ratio > 0.45:
            return False
        return True

    if side == "SHORT":
        if c >= o:
            return False
        if lower_ratio > 0.45:
            return False
        return True

    return False


# =========================
# Data fetch (IEX to avoid SIP)
# =========================
def fetch_last_minute_bars(hist: StockHistoricalDataClient, symbol: str, minutes: int) -> pd.DataFrame:
    end = now_utc()
    start = end - timedelta(minutes=minutes + 5)

    req = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed="iex",   # ✅ أهم سطر: يمنع خطأ SIP
    )
    bars = hist.get_stock_bars(req).df
    if bars is None or len(bars) == 0:
        return pd.DataFrame()

    # df يكون MultiIndex (symbol, timestamp)
    try:
        df = bars.xs(symbol)
    except Exception:
        df = bars.copy()

    df = df.sort_index()
    return df


def compute_signal(df: pd.DataFrame) -> Optional[dict]:
    if df is None or df.empty:
        return None
    if len(df) < max(MA_MIN + 2, RECENT_WINDOW_MIN + 2):
        return None

    # آخر شمعة دقيقة
    last = df.iloc[-1]
    price = float(last["close"])

    # MA
    ma = float(df["close"].tail(MA_MIN).mean())
    if ma <= 0:
        return None

    diff_pct = (price - ma) / ma  # موجب = فوق المتوسط

    # Volume spike baseline
    v_last = float(last["volume"])
    v_base = float(df["volume"].iloc[-(RECENT_WINDOW_MIN+1):-1].mean())
    if v_base <= 0:
        return None
    vol_ratio = v_last / v_base

    # Recent move (آخر 10 دقائق)
    w = df["close"].tail(RECENT_WINDOW_MIN)
    recent_move_pct = (float(w.iloc[-1]) - float(w.iloc[0])) / float(w.iloc[0])

    # شروط أساسية
    if abs(diff_pct) < MIN_DIFF_PCT:
        return None
    if vol_ratio < MIN_VOL_RATIO:
        return None
    if abs(recent_move_pct) > MAX_RECENT_MOVE_PCT:
        return None

    side = "LONG" if diff_pct > 0 else "SHORT"

    # Candle filter
    if CANDLE_FILTER != "OFF":
        o = float(last["open"])
        h = float(last["high"])
        l = float(last["low"])
        c = float(last["close"])
        if not candle_filter_light(o, h, l, c, side):
            return {
                "side": side,
                "price": price,
                "ma": ma,
                "diff_pct": diff_pct,
                "vol_ratio": vol_ratio,
                "v_last": v_last,
                "v_base": v_base,
                "recent_move_pct": recent_move_pct,
                "candle_pass": False,
            }

    return {
        "side": side,
        "price": price,
        "ma": ma,
        "diff_pct": diff_pct,
        "vol_ratio": vol_ratio,
        "v_last": v_last,
        "v_base": v_base,
        "recent_move_pct": recent_move_pct,
        "candle_pass": True,
    }


# =========================
# Trading (optional)
# =========================
def place_trade(trade: TradingClient, symbol: str, side: str, price: float) -> str:
    # كمية تقريبية حسب USD_PER_TRADE
    qty = max(int(USD_PER_TRADE / max(price, 0.01)), 1)

    order_side = OrderSide.BUY if side == "LONG" else OrderSide.SELL

    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
    )
    o = trade.submit_order(req)
    return f"ORDER SENT: {symbol} {side} qty={qty} (market)"


# =========================
# Main loop
# =========================
def main():
    hist, trade, paper = build_clients()
    symbols = parse_symbols()

    send_telegram(
        f"✅ Bot started ({MODE}) | symbols={','.join(symbols)} | interval={INTERVAL_SEC}s | feed=IEX | paper={paper} | candle={CANDLE_FILTER}"
    )

    last_sent = {}  # symbol -> timestamp

    while True:
        try:
            for sym in symbols:
                df = fetch_last_minute_bars(hist, sym, minutes=max(RECENT_WINDOW_MIN, MA_MIN) + 2)
                sig = compute_signal(df)
                if not sig:
                    continue

                # لو الشمعة ما نجحت نرسل تنبيه مختصر فقط (للتوضيح)
                candle_txt = "PASS" if sig.get("candle_pass") else "FAIL"
                side = sig["side"]

                # منع تكرار نفس السهم بسرعة
                key = f"{sym}:{side}:{candle_txt}"
                tnow = time.time()
                if key in last_sent and (tnow - last_sent[key]) < 60:
                    continue
                last_sent[key] = tnow

                msg = (
                    f"📣 Signal: {side} | {sym}\n"
                    f"Price: {sig['price']:.2f}\n"
                    f"MA({MA_MIN}m): {sig['ma']:.2f}\n"
                    f"Diff: {sig['diff_pct']*100:.2f}%\n"
                    f"Volume Spike: {int(sig['v_last'])} vs avg {int(sig['v_base'])} (x{sig['vol_ratio']:.2f})\n"
                    f"Recent Move ({RECENT_WINDOW_MIN}m): {sig['recent_move_pct']*100:.2f}%\n"
                    f"🕯 Candle Filter (LIGHT): {candle_txt}\n"
                    f"Time (UTC): {now_utc().strftime('%Y-%m-%d %H:%M:%S')}"
                )

                send_telegram(msg)

                # التداول فقط إذا MODE=TRADE و AUTO_TRADING=true و الشمعة PASS
                if MODE == "TRADE" and AUTO_TRADING and sig.get("candle_pass"):
                    try:
                        status = place_trade(trade, sym, side, sig["price"])
                        send_telegram("🚀 " + status)
                    except Exception as e:
                        send_telegram(f"⚠️ Trade error: {type(e).__name__}: {e}")

        except Exception as e:
            # أهم شيء: لا يطفّي السيرفس
            send_telegram(f"⚠️ Bot error: {type(e).__name__}: {e}")
            print("Bot error:", type(e).__name__, e)

        time.sleep(INTERVAL_SEC)


if __name__ == "__main__":
    main()
