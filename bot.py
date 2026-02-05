import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.requests import TakeProfitRequest, StopLossRequest

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.data.enums import DataFeed


# =========================
# Helpers
# =========================
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()

def env_float(name: str, default: str) -> float:
    try:
        return float(env(name, default))
    except Exception:
        raise RuntimeError(f"Invalid float env var: {name}")

def env_int(name: str, default: str) -> int:
    try:
        return int(env(name, default))
    except Exception:
        raise RuntimeError(f"Invalid int env var: {name}")

def env_bool(name: str, default: str = "false") -> bool:
    v = os.getenv(name, default)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_symbols() -> list[str]:
    raw = os.getenv("SYMBOLS", "TSLA,AAPL,NVDA,AMD,AMZN,GOOGL,MU,MSFT")
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]

def send_telegram(text: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text[:200]}")

def safe_send_telegram(text: str) -> None:
    try:
        send_telegram(text)
    except Exception:
        # آخر شيء نبغاه كراش بسبب التليجرام
        pass


# =========================
# Candle filter (LIGHT)
# =========================
def candle_filter_light(side: OrderSide, o: float, h: float, l: float, c: float) -> tuple[bool, str]:
    """
    فلتر شمعة خفيف:
    LONG: شمعة خضراء + جسم واضح + إغلاق قريب من الأعلى
    SHORT: شمعة حمراء + جسم واضح + إغلاق قريب من الأسفل
    """
    rng = max(h - l, 1e-9)
    body = abs(c - o)
    body_ratio = body / rng

    # close position inside range
    close_pos = (c - l) / rng  # 0..1

    if side == OrderSide.BUY:
        if c <= o:
            return False, "Not green"
        if body_ratio < 0.45:
            return False, f"Body small ({body_ratio:.2f})"
        if close_pos < 0.65:
            return False, f"Close not near high ({close_pos:.2f})"
        return True, "PASS"
    else:
        if c >= o:
            return False, "Not red"
        if body_ratio < 0.45:
            return False, f"Body small ({body_ratio:.2f})"
        if close_pos > 0.35:
            return False, f"Close not near low ({close_pos:.2f})"
        return True, "PASS"


# =========================
# Alpaca clients
# =========================
def build_clients() -> tuple[StockHistoricalDataClient, TradingClient, bool]:
    api_key = env("ALPACA_API_KEY")
    secret = env("ALPACA_SECRET_KEY")

    paper_mode = env_bool("ALPACA_PAPER", "true")  # true = paper

    data_client = StockHistoricalDataClient(api_key, secret)
    trade_client = TradingClient(api_key, secret, paper=paper_mode)

    return data_client, trade_client, paper_mode


# =========================
# Market data
# =========================
def get_bars(
    data_client: StockHistoricalDataClient,
    symbol: str,
    tf_min: int,
    limit: int,
    feed: DataFeed,
) -> list:
    end = now_utc()
    start = end - timedelta(minutes=tf_min * (limit + 5))

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame(tf_min, TimeFrameUnit.Minute),
        start=start,
        end=end,
        limit=limit,
        feed=feed,  # ✅ feed هنا (مو في StockHistoricalDataClient)
    )
    resp = data_client.get_stock_bars(req)

    # resp[symbol] عبارة عن list of Bar
    bars = resp[symbol] if symbol in resp else []
    return list(bars)


# =========================
# Strategy
# =========================
def compute_signal(bars: list, ma_window: int) -> tuple[OrderSide | None, dict]:
    """
    يرجّع LONG/SHORT/None + معلومات للعرض
    """
    if len(bars) < max(ma_window, 10):
        return None, {"reason": "Not enough bars"}

    closes = [float(b.close) for b in bars]
    opens = [float(b.open) for b in bars]
    highs = [float(b.high) for b in bars]
    lows  = [float(b.low) for b in bars]
    vols  = [float(b.volume) for b in bars]

    last_c = closes[-1]
    last_o = opens[-1]
    last_h = highs[-1]
    last_l = lows[-1]
    last_v = vols[-1]

    ma = sum(closes[-ma_window:]) / ma_window
    diff = (last_c - ma) / ma if ma != 0 else 0.0

    info = {
        "price": last_c,
        "ma": ma,
        "diff": diff,
        "o": last_o,
        "h": last_h,
        "l": last_l,
        "c": last_c,
        "v": last_v,
    }

    # LONG إذا السعر فوق MA، SHORT إذا تحت MA
    side = OrderSide.BUY if diff > 0 else OrderSide.SELL
    return side, info


def volume_spike_ok(vols: list[float], lookback: int, min_ratio: float) -> tuple[bool, float, float, float]:
    if len(vols) < lookback + 1:
        return False, 0.0, 0.0, 0.0
    baseline = sum(vols[-(lookback+1):-1]) / lookback
    last_v = vols[-1]
    ratio = (last_v / baseline) if baseline > 0 else 0.0
    return ratio >= min_ratio, last_v, baseline, ratio


def recent_move_ok(closes: list[float], window: int, max_abs_move: float) -> tuple[bool, float]:
    """
    نحسب حركة آخر window شموع (تقريبًا) بالنسبة للسعر الحالي
    """
    if len(closes) < window + 1:
        return True, 0.0
    prev = closes[-(window + 1)]
    last = closes[-1]
    move = (last - prev) / prev if prev != 0 else 0.0
    return abs(move) <= max_abs_move, move


# =========================
# Trading (optional)
# =========================
def place_trade(
    trade_client: TradingClient,
    symbol: str,
    side: OrderSide,
    usd_per_trade: float,
    price: float,
    take_profit_pct: float,
    stop_loss_pct: float,
) -> str:
    qty = int(max(1, math.floor(usd_per_trade / max(price, 1e-9))))

    tp_price = None
    sl_price = None

    if take_profit_pct > 0:
        tp_price = price * (1.0 + take_profit_pct) if side == OrderSide.BUY else price * (1.0 - take_profit_pct)
    if stop_loss_pct > 0:
        sl_price = price * (1.0 - stop_loss_pct) if side == OrderSide.BUY else price * (1.0 + stop_loss_pct)

    take_profit = TakeProfitRequest(limit_price=round(tp_price, 2)) if tp_price else None
    stop_loss = StopLossRequest(stop_price=round(sl_price, 2)) if sl_price else None

    order = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=side,
        time_in_force=TimeInForce.DAY,
        take_profit=take_profit,
        stop_loss=stop_loss,
    )
    res = trade_client.submit_order(order)
    return f"Order placed: {symbol} {side.value} qty={qty}"


# =========================
# Main loop
# =========================
def main() -> None:
    # ---- config ----
    symbols = parse_symbols()

    mode = os.getenv("MODE", "ALERTS").strip().upper()   # ALERTS أو AUTO
    auto_trade = env_bool("AUTO_TRADE", "false") or (mode == "AUTO")

    interval_sec = env_int("INTERVAL_SEC", "15")

    tf_min = env_int("TF_MIN", "3")               # 3 دقائق (زي رسالتك)
    ma_window = env_int("MA_WINDOW", "5")         # MA على 5 شموع
    vol_lookback = env_int("VOL_LOOKBACK", "20")  # baseline volume
    min_vol_ratio = env_float("MIN_VOL_RATIO", "1.4")

    recent_window = env_int("RECENT_WINDOW", "10")
    max_recent_move = env_float("MAX_RECENT_MOVE_PCT", "0.003")  # 0.3%

    min_diff = env_float("MIN_DIFF_PCT", "0.0010")   # 0.10%
    max_diff = env_float("MAX_DIFF_PCT", "0.0030")   # 0.30%

    candle_filter = os.getenv("CANDLE_FILTER", "LIGHT").strip().upper()

    usd_per_trade = env_float("USD_PER_TRADE", "2000")
    take_profit_pct = env_float("TAKE_PROFIT_PCT", "0.0025")  # 0.25%
    stop_loss_pct = env_float("STOP_LOSS_PCT", "0.0015")      # 0.15%

    # ✅ data feed: الافتراضي IEX عشان ما يطلع خطأ SIP
    feed_name = os.getenv("DATA_FEED", "IEX").strip().upper()
    data_feed = DataFeed.IEX if feed_name == "IEX" else DataFeed.SIP

    # ---- clients ----
    data_client, trade_client, paper_mode = build_clients()

    safe_send_telegram(
        "✅ Bot started\n"
        f"mode={mode} | auto_trade={auto_trade} | paper={paper_mode}\n"
        f"symbols={','.join(symbols)} | tf={tf_min}m | interval={interval_sec}s | feed={data_feed.value}\n"
        f"candle={candle_filter} | MIN_VOL_RATIO={min_vol_ratio} | diff=[{min_diff*100:.2f}%..{max_diff*100:.2f}%]"
    )

    last_signal_time: dict[str, float] = {}

    while True:
        try:
            for sym in symbols:
                bars = get_bars(data_client, sym, tf_min=tf_min, limit=max(vol_lookback + 5, 60), feed=data_feed)

                if not bars:
                    continue

                closes = [float(b.close) for b in bars]
                vols = [float(b.volume) for b in bars]

                side, info = compute_signal(bars, ma_window=ma_window)
                if side is None:
                    continue

                diff = float(info["diff"])
                abs_diff = abs(diff)

                # diff filter
                if abs_diff < min_diff or abs_diff > max_diff:
                    continue

                # volume spike
                ok_vol, last_v, baseline, ratio = volume_spike_ok(vols, lookback=vol_lookback, min_ratio=min_vol_ratio)
                if not ok_vol:
                    continue

                # recent move filter
                ok_move, move = recent_move_ok(closes, window=recent_window, max_abs_move=max_recent_move)
                if not ok_move:
                    continue

                # candle filter (LIGHT)
                candle_ok = True
                candle_reason = "SKIP"
                if candle_filter == "LIGHT":
                    candle_ok, candle_reason = candle_filter_light(
                        side=side,
                        o=float(info["o"]),
                        h=float(info["h"]),
                        l=float(info["l"]),
                        c=float(info["c"]),
                    )
                    if not candle_ok:
                        continue

                # avoid spam: one signal per symbol per 45s
                now_ts = time.time()
                if sym in last_signal_time and (now_ts - last_signal_time[sym]) < 45:
                    continue
                last_signal_time[sym] = now_ts

                direction = "LONG" if side == OrderSide.BUY else "SHORT"
                strength = "متوسطة ✅"

                msg = (
                    f"📣 Signal: {direction} | {sym}\n"
                    f"Price: {info['price']:.2f}\n"
                    f"MA({tf_min}m): {info['ma']:.2f}\n"
                    f"Diff: {diff*100:.2f}%\n"
                    f"🔥 Volume Spike (baseline): {last_v:.0f} مقابل {baseline:.0f} (x{ratio:.2f})\n"
                    f"🧠 Recent Move ({recent_window}): {move*100:.2f}%\n"
                    f"🕯 Candle Filter (LIGHT): {candle_reason}\n"
                    f"⭐ Strength: {strength}\n"
                    f"⏱ Time (UTC): {now_utc().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                safe_send_telegram(msg)

                # auto trade (optional)
                if auto_trade:
                    try:
                        placed = place_trade(
                            trade_client=trade_client,
                            symbol=sym,
                            side=side,
                            usd_per_trade=usd_per_trade,
                            price=float(info["price"]),
                            take_profit_pct=take_profit_pct,
                            stop_loss_pct=stop_loss_pct,
                        )
                        safe_send_telegram(f"✅ {placed}")
                    except Exception as e:
                        safe_send_telegram(f"⚠️ Trade error: {type(e).__name__}: {str(e)[:180]}")

            time.sleep(interval_sec)

        except Exception as e:
            # أي خطأ في الداتا/الاشتراك/الخ.. نرسله بدون ما نطيّح البوت
            safe_send_telegram(f"⚠️ Bot error: {type(e).__name__}: {str(e)[:200]}")
            time.sleep(10)


if __name__ == "__main__":
    main()
