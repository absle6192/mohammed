import os
import time
import math
import requests
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional, Tuple

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
def _env(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def _env_any(*names: str, default: Optional[str] = None) -> str:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    if default is not None:
        return default
    raise RuntimeError(f"Missing env var (any of): {', '.join(names)}")


def _env_int(name: str, default: str) -> int:
    try:
        return int(_env(name, default))
    except Exception:
        raise RuntimeError(f"Invalid int for {name}")


def _env_float(name: str, default: str) -> float:
    try:
        return float(_env(name, default))
    except Exception:
        raise RuntimeError(f"Invalid float for {name}")


def _env_bool(name: str, default: str = "false") -> bool:
    v = os.getenv(name, default)
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def parse_symbols() -> List[str]:
    raw = os.getenv("SYMBOLS", "TSLA,AAPL,NVDA,AMD,AMZN,GOOGL,MU,MSFT")
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]


def send_telegram(text: str) -> None:
    token = _env_any("TELEGRAM_BOT_TOKEN")
    chat_id = _env_any("TELEGRAM_CHAT_ID")

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        "disable_notification": False,
    }
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text}")


# =========================
# Candle filter (LIGHT)
# =========================
def candle_pass_light(df: pd.DataFrame, side: str) -> bool:
    """
    Light filter:
    - LONG: last candle green with decent body and not huge upper wick
    - SHORT: last candle red with decent body and not huge lower wick
    """
    if df is None or len(df) < 2:
        return False

    last = df.iloc[-1]
    o = float(last["open"])
    h = float(last["high"])
    l = float(last["low"])
    c = float(last["close"])

    rng = max(h - l, 1e-9)
    body = abs(c - o)
    body_ratio = body / rng

    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l

    upper_ratio = upper_wick / rng
    lower_ratio = lower_wick / rng

    # لازم جسم واضح
    if body_ratio < 0.35:
        return False

    if side == "LONG":
        # لازم خضراء
        if c <= o:
            return False
        # لا يكون ظل علوي كبير
        if upper_ratio > 0.45:
            return False
        return True

    if side == "SHORT":
        # لازم حمراء
        if c >= o:
            return False
        # لا يكون ظل سفلي كبير
        if lower_ratio > 0.45:
            return False
        return True

    return False


# =========================
# Alpaca clients (FIXED)
# =========================
def build_clients() -> Tuple[StockHistoricalDataClient, TradingClient]:
    """
    مفاتيحك في Render (مثل ما عندك):
      APCA_API_KEY_ID
      APCA_API_SECRET_KEY
      APCA_API_BASE_URL  (موجود عندك لكن ما نمرره للـ SDK هنا)
    """
    api_key = _env_any("APCA_API_KEY_ID")
    secret = _env_any("APCA_API_SECRET_KEY")

    # paper من env (اختياري) — إذا ما حطيته نخليه True لأن حسابك تجريبي
    paper = _env_bool("PAPER", "true")

    hist = StockHistoricalDataClient(api_key, secret)              # ✅ بدون feed=
    trade = TradingClient(api_key, secret, paper=paper)            # ✅ بدون base_url=

    return hist, trade


# =========================
# Strategy params
# =========================
@dataclass
class Params:
    interval_sec: int
    ma_minutes: int
    recent_window_min: int
    min_diff_pct: float
    max_recent_move_pct: float
    min_vol_ratio: float
    candle_mode: str  # OFF / LIGHT
    mode: str         # ALERTS / TRADE
    auto_trade: bool
    usd_per_trade: float


def load_params() -> Params:
    return Params(
        interval_sec=_env_int("INTERVAL_SEC", "15"),
        ma_minutes=_env_int("MA_MINUTES", "3"),
        recent_window_min=_env_int("RECENT_WINDOW_MIN", "10"),
        min_diff_pct=_env_float("MIN_DIFF_PCT", "0.0010"),          # 0.10%
        max_recent_move_pct=_env_float("MAX_RECENT_MOVE_PCT", "0.0030"),  # 0.30%
        min_vol_ratio=_env_float("MIN_VOL_RATIO", "1.4"),
        candle_mode=_env("CANDLE", "LIGHT").strip().upper(),
        mode=_env("MODE", "ALERTS").strip().upper(),
        auto_trade=_env_bool("AUTO_TRADE", "false") or (_env("AUTO_TRADE", "off").strip().lower() == "on"),
        usd_per_trade=_env_float("USD_PER_TRADE", "2000"),
    )


# =========================
# Data fetch (IEX feed)
# =========================
def fetch_minute_bars(hist: StockHistoricalDataClient, symbol: str, minutes: int) -> pd.DataFrame:
    """
    نجلب بيانات دقيقة من Alpaca Data.
    لتفادي SIP error: نطلب feed=iex داخل StockBarsRequest.
    """
    end = now_utc()
    start = end - pd.Timedelta(minutes=minutes + 5)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start.to_pydatetime(),
        end=end.to_pydatetime(),
        feed="iex",  # ✅ هنا مكان feed الصحيح (مو في client)
    )

    bars = hist.get_stock_bars(req).df
    if bars is None or len(bars) == 0:
        return pd.DataFrame()

    # إذا رجع multi-index، خلّه بسيط
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.reset_index()
        bars = bars[bars["symbol"] == symbol].copy()
    else:
        bars = bars.reset_index()

    # تأكد الأعمدة
    # expected: timestamp, open, high, low, close, volume
    cols = {c.lower(): c for c in bars.columns}
    need = ["open", "high", "low", "close", "volume"]
    for n in need:
        if n not in cols:
            # حاول أسماء بديلة
            pass

    # خله مرتب
    if "timestamp" in bars.columns:
        bars = bars.sort_values("timestamp")
    return bars


def compute_signal(df: pd.DataFrame, p: Params) -> Optional[dict]:
    if df is None or len(df) < max(p.ma_minutes, p.recent_window_min) + 2:
        return None

    # last price
    price = float(df.iloc[-1]["close"])

    # MA over last N minutes
    ma = float(df.tail(p.ma_minutes)["close"].mean())

    diff = (price - ma) / ma  # + فوق / - تحت

    # volume spike (last candle vs avg recent)
    last_vol = float(df.iloc[-1]["volume"])
    avg_vol = float(df.tail(p.recent_window_min)["volume"].mean())
    vol_ratio = (last_vol / avg_vol) if avg_vol > 0 else 0.0

    # recent move (absolute over window)
    recent_close = float(df.iloc[-p.recent_window_min]["close"])
    recent_move = (price - recent_close) / recent_close if recent_close != 0 else 0.0

    # Decide side by diff direction
    side = None
    if diff >= p.min_diff_pct:
        side = "LONG"
    elif diff <= -p.min_diff_pct:
        side = "SHORT"
    else:
        return None

    # filters
    if vol_ratio < p.min_vol_ratio:
        return None

    if abs(recent_move) > p.max_recent_move_pct:
        return None

    # candle filter
    candle_ok = True
    if p.candle_mode == "LIGHT":
        candle_ok = candle_pass_light(df, side)

    return {
        "side": side,
        "price": price,
        "ma": ma,
        "diff": diff,
        "last_vol": last_vol,
        "avg_vol": avg_vol,
        "vol_ratio": vol_ratio,
        "recent_move": recent_move,
        "candle_ok": candle_ok,
    }


# =========================
# Trading (optional)
# =========================
def qty_from_usd(usd: float, price: float) -> int:
    if price <= 0:
        return 0
    q = int(math.floor(usd / price))
    return max(q, 0)


def maybe_trade(trade_client: TradingClient, symbol: str, sig: dict, p: Params) -> Optional[str]:
    """
    يتداول فقط إذا:
      MODE=TRADE AND AUTO_TRADE=on/true
    غير كذا: إشعارات فقط.
    """
    if not (p.mode == "TRADE" and p.auto_trade):
        return None

    qty = qty_from_usd(p.usd_per_trade, float(sig["price"]))
    if qty <= 0:
        return "Skip trade: qty=0"

    side = OrderSide.BUY if sig["side"] == "LONG" else OrderSide.SELL

    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
    )
    o = trade_client.submit_order(req)
    return f"✅ Order sent: {symbol} {sig['side']} qty={qty} (id={getattr(o, 'id', 'n/a')})"


# =========================
# Main loop
# =========================
def main() -> None:
    p = load_params()
    symbols = parse_symbols()

    hist, trade_client = build_clients()

    # رسالة تأكيد تشغيل
    send_telegram(
        f"✅ Bot started ({p.mode}) | symbols={','.join(symbols)} | interval={p.interval_sec}s | "
        f"paper={_env_bool('PAPER','true')} | candle={p.candle_mode} | AUTO_TRADE={'on' if p.auto_trade else 'off'}"
    )

    last_sent = {}  # symbol -> last side to avoid spam

    while True:
        try:
            for sym in symbols:
                df = fetch_minute_bars(hist, sym, minutes=max(p.recent_window_min, p.ma_minutes) + 20)
                if df.empty:
                    continue

                sig = compute_signal(df, p)
                if not sig:
                    continue

                # candle must pass if enabled
                if p.candle_mode == "LIGHT" and not sig["candle_ok"]:
                    continue

                # avoid duplicate spam: same side within short time
                key = (sym, sig["side"])
                if last_sent.get(sym) == sig["side"]:
                    continue

                last_sent[sym] = sig["side"]

                msg = (
                    f"📣 Signal: {sig['side']} | {sym}\n"
                    f"Price: {sig['price']:.2f}\n"
                    f"MA({p.ma_minutes}m): {sig['ma']:.2f}\n"
                    f"Diff: {sig['diff']*100:.2f}%\n"
                    f"Volume Spike: {int(sig['last_vol'])} vs avg {int(sig['avg_vol'])} (x{sig['vol_ratio']:.2f})\n"
                    f"Recent Move ({p.recent_window_min}m): {sig['recent_move']*100:.2f}%\n"
                    f"Candle Filter ({p.candle_mode}): {'PASS' if sig['candle_ok'] else 'FAIL'}\n"
                    f"Time(UTC): {now_utc().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                send_telegram(msg)

                trade_msg = maybe_trade(trade_client, sym, sig, p)
                if trade_msg:
                    send_telegram(trade_msg)

            time.sleep(p.interval_sec)

        except Exception as e:
            # لا ينهار البوت، بس يرسل خطأ ويكمل
            try:
                send_telegram(f"⚠️ Bot error: {type(e).__name__}: {e}")
            except Exception:
                pass
            time.sleep(max(10, p.interval_sec))


if __name__ == "__main__":
    main()
