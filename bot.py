import os
import time
import math
import requests
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce, OrderClass
from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest


# ======================
#        HELPERS
# ======================
def env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()

def env_float(name: str, default: str) -> float:
    return float(env(name, default))

def env_int(name: str, default: str) -> int:
    return int(env(name, default))

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def send_telegram(text: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception:
        # لا نطيح البوت بسبب تيليجرام
        pass


# ======================
#       STRATEGY
# ======================
@dataclass
class Signal:
    symbol: str
    side: str  # "LONG" or "SHORT"
    price: float
    ma_5m: float
    diff_pct: float
    vol: float
    vol_avg: float
    vol_ratio: float
    time_utc: str

def clamp_qty(qty: int) -> int:
    return max(1, int(qty))

def round2(x: float) -> float:
    return float(f"{x:.2f}")

def is_strong_candle_light(df_1m: pd.DataFrame, side: str) -> bool:
    """
    فلتر شموع خفيف:
    LONG: شمعة خضراء بجسم واضح، ظل علوي مو طويل
    SHORT: شمعة حمراء بجسم واضح، ظل سفلي مو طويل
    """
    if df_1m is None or len(df_1m) < 3:
        return False

    last = df_1m.iloc[-1]
    o, h, l, c = float(last["open"]), float(last["high"]), float(last["low"]), float(last["close"])
    rng = max(1e-9, h - l)
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    body_ratio = body / rng
    upper_ratio = upper_wick / rng
    lower_ratio = lower_wick / rng

    # جسم واضح
    if body_ratio < 0.35:
        return False

    if side == "LONG":
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


# ======================
#     ALPACA CLIENTS
# ======================
def build_clients() -> Tuple[StockHistoricalDataClient, TradingClient]:
    """
    يقبل المفاتيح بأي من النظامين:
    - الجديد: ALPACA_API_KEY / ALPACA_SECRET_KEY
    - القديم: APCA_API_KEY_ID / APCA_API_SECRET_KEY
    (بدون ما نضيف ولا نكرر مفاتيح في Render)
    """
    api_key = os.getenv("ALPACA_API_KEY") or os.getenv("APCA_API_KEY_ID")
    secret  = os.getenv("ALPACA_SECRET_KEY") or os.getenv("APCA_API_SECRET_KEY")

    if not api_key or not secret:
        raise RuntimeError(
            "Missing Alpaca keys. Set either "
            "ALPACA_API_KEY/ALPACA_SECRET_KEY or APCA_API_KEY_ID/APCA_API_SECRET_KEY"
        )

    paper = env("ALPACA_PAPER", "true").lower() in ("1", "true", "yes", "y")

    hist = StockHistoricalDataClient(api_key, secret)
    trade = TradingClient(api_key, secret, paper=paper)
    return hist, trade


def get_bars_1m(hist: StockHistoricalDataClient, symbol: str, minutes: int) -> pd.DataFrame:
    end = now_utc()
    start = end - timedelta(minutes=minutes + 2)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        adjustment="raw",
    )

    bars = hist.get_stock_bars(req).df
    if bars is None or len(bars) == 0:
        return pd.DataFrame()

    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.xs(symbol)

    return bars.sort_index()


def compute_signal(symbol: str, df_1m: pd.DataFrame) -> Optional[Signal]:
    if df_1m is None or len(df_1m) < 20:
        return None

    last_close = float(df_1m["close"].iloc[-1])
    ma_5m = float(df_1m["close"].iloc[-5:].mean())

    diff_pct = (last_close - ma_5m) / ma_5m

    vol = float(df_1m["volume"].iloc[-1])
    vol_window = env_int("VOL_AVG_WINDOW", "20")
    vol_avg = float(df_1m["volume"].iloc[-vol_window:].mean())
    vol_ratio = vol / max(1e-9, vol_avg)

    min_vol_ratio = env_float("MIN_VOL_RATIO", "1.4")
    min_diff_pct = env_float("MIN_DIFF_PCT", "0.0010")

    if vol_ratio < min_vol_ratio:
        return None

    if diff_pct >= min_diff_pct:
        side = "LONG"
    elif diff_pct <= -min_diff_pct:
        side = "SHORT"
    else:
        return None

    return Signal(
        symbol=symbol,
        side=side,
        price=round2(last_close),
        ma_5m=round2(ma_5m),
        diff_pct=diff_pct,
        vol=vol,
        vol_avg=vol_avg,
        vol_ratio=vol_ratio,
        time_utc=now_utc().strftime("%Y-%m-%d %H:%M:%S"),
    )


def too_big_jump(df_1m: pd.DataFrame) -> bool:
    if df_1m is None or len(df_1m) < 1:
        return True
    last = df_1m.iloc[-1]
    o, c = float(last["open"]), float(last["close"])
    jump_pct = abs(c - o) / max(1e-9, o)
    max_jump = env_float("MAX_JUMP_PCT", "0.0030")
    return jump_pct > max_jump


# ======================
#     TRADING UTILS
# ======================
def calc_qty_by_usd(price: float) -> int:
    usd = env_float("USD_PER_TRADE", "2000")
    qty = int(math.floor(usd / max(1e-9, price)))
    return clamp_qty(qty)

def compute_pullback_entry(side: str, last_price: float) -> float:
    pb_pct = env_float("PULLBACK_PCT", "0.0008")
    spread_guard = env_float("SPREAD_GUARD_PCT", "0.0003")

    if side == "LONG":
        entry = min(last_price * (1.0 - pb_pct), last_price * (1.0 - spread_guard))
    else:
        entry = max(last_price * (1.0 + pb_pct), last_price * (1.0 + spread_guard))

    return round2(entry)

def place_bracket_limit(trading: TradingClient, symbol: str, side: str, qty: int, entry: float) -> str:
    tp_pct = env_float("TAKE_PROFIT_PCT", "0.0025")
    sl_pct = env_float("STOP_LOSS_PCT", "0.0015")

    if side == "LONG":
        take_profit = entry * (1.0 + tp_pct)
        stop_loss = entry * (1.0 - sl_pct)
        order_side = OrderSide.BUY
    else:
        take_profit = entry * (1.0 - tp_pct)
        stop_loss = entry * (1.0 + sl_pct)
        order_side = OrderSide.SELL

    req = LimitOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=TimeInForce.DAY,
        limit_price=entry,
        order_class=OrderClass.BRACKET,
        take_profit=TakeProfitRequest(limit_price=round2(take_profit)),
        stop_loss=StopLossRequest(stop_price=round2(stop_loss)),
    )
    order = trading.submit_order(req)
    return order.id


# ======================
#          MAIN
# ======================
def main():
    # يقبل SYMBOLS أو TICKERS (عشان ما تتعب بتغيير الاسم)
    symbols_raw = os.getenv("SYMBOLS") or os.getenv("TICKERS") or "TSLA,NVDA,AAPL,AMD,AMZN,GOOGL,MU,MSFT"
    symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]

    poll_sec = env_int("POLL_SEC", "5")

    hist, trading = build_clients()

    send_telegram("✅ أسهم نجد: البوت اشتغل (Signals + Optional Trading)")

    while True:
        try:
            for sym in symbols:
                df = get_bars_1m(hist, sym, minutes=60)
                if df.empty:
                    continue

                sig = compute_signal(sym, df)
                if sig is None:
                    continue

                # حماية القفز
                if too_big_jump(df):
                    send_telegram(
                        f"🚫 IGNORE (Jump)\n{sig.symbol} {sig.side}\n"
                        f"Price: {sig.price} | MA(5m): {sig.ma_5m}\n"
                        f"Diff: {sig.diff_pct*100:.2f}% | Vol x{sig.vol_ratio:.2f}\n"
                        f"Time(UTC): {sig.time_utc}"
                    )
                    continue

                # فلتر شموع خفيف
                if not is_strong_candle_light(df, sig.side):
                    send_telegram(
                        f"⚠️ FILTERED (Candle)\n{sig.symbol} {sig.side}\n"
                        f"Price: {sig.price} | MA(5m): {sig.ma_5m}\n"
                        f"Diff: {sig.diff_pct*100:.2f}% | Vol x{sig.vol_ratio:.2f}\n"
                        f"Time(UTC): {sig.time_utc}"
                    )
                    continue

                # إشعار الإشارة
                send_telegram(
                    f"📣 Signal: {sig.side} | {sig.symbol}\n"
                    f"Price: {sig.price}\n"
                    f"MA(5m): {sig.ma_5m}\n"
                    f"Diff: {sig.diff_pct*100:.2f}%\n"
                    f"Vol spike: {int(sig.vol)} vs avg {int(sig.vol_avg)} (x{sig.vol_ratio:.2f})\n"
                    f"Time(UTC): {sig.time_utc}"
                )

                # تنفيذ تداول فقط إذا MODE=TRADE
                mode = (os.getenv("MODE") or "ALERTS").upper()
                if mode != "TRADE":
                    continue

                entry = compute_pullback_entry(sig.side, sig.price)
                qty = calc_qty_by_usd(sig.price)
                order_id = place_bracket_limit(trading, sym, sig.side, qty, entry)

                send_telegram(
                    f"✅ ORDER SENT (Bracket Limit)\n"
                    f"{sym} {sig.side}\n"
                    f"Qty: {qty}\n"
                    f"Entry(limit): {entry}\n"
                    f"TP/SL: {env_float('TAKE_PROFIT_PCT','0.0025')*100:.2f}% / {env_float('STOP_LOSS_PCT','0.0015')*100:.2f}%\n"
                    f"OrderId: {order_id}"
                )

            time.sleep(poll_sec)

        except Exception as e:
            send_telegram(f"❌ ERROR: {type(e).__name__}: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
