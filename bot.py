import os
import time
import math
import requests
from datetime import datetime, timezone, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce


# ----------------- ENV helpers -----------------
def env_any(*names: str, default: str | None = None) -> str:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    if default is None:
        raise RuntimeError(f"Missing env var (any of): {names}")
    return str(default).strip()


def env_float(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        raise RuntimeError(f"Invalid float for {name}")


def env_int(name: str, default: str) -> int:
    try:
        return int(os.getenv(name, default))
    except Exception:
        raise RuntimeError(f"Invalid int for {name}")


def env_bool(name: str, default: str = "false") -> bool:
    v = os.getenv(name, default)
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


# ----------------- Telegram -----------------
def send_telegram(text: str) -> None:
    token = env_any("TELEGRAM_BOT_TOKEN")
    chat_id = env_any("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }

    r = requests.post(url, json=payload, timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text[:200]}")


# ----------------- Config -----------------
def parse_symbols() -> list[str]:
    raw = os.getenv("SYMBOLS", "TSLA,AAPL,NVDA,AMD,AMZN,GOOGL,MU,MSFT")
    parts = [p.strip().upper() for p in raw.split(",")]
    return [p for p in parts if p]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ----------------- Alpaca clients -----------------
def build_clients() -> tuple[StockHistoricalDataClient, TradingClient, bool]:
    # يدعم مفاتيح Alpaca بالنظامين:
    # الجديد: ALPACA_API_KEY / ALPACA_SECRET_KEY
    # القديم: APCA_API_KEY_ID / APCA_API_SECRET_KEY
    api_key = env_any("ALPACA_API_KEY", "APCA_API_KEY_ID")
    secret = env_any("ALPACA_SECRET_KEY", "APCA_API_SECRET_KEY")

    # PAPER من Render عندك غالباً true
    paper = env_bool("ALPACA_PAPER", "true")

    hist = StockHistoricalDataClient(api_key, secret)

    # TradingClient ما نطبع منه paper لأنه ما عنده attribute paper
    trading = TradingClient(api_key, secret, paper=paper)

    return hist, trading, paper


# ----------------- Signal logic (Alerts) -----------------
def get_last_bars(hist: StockHistoricalDataClient, symbol: str, minutes: int = 15):
    end = now_utc()
    start = end - timedelta(minutes=minutes + 5)

    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=TimeFrame.Minute,
        start=start,
        end=end,
        feed=DataFeed.IEX,   # ✅ يمنع خطأ SIP
    )
    bars = hist.get_stock_bars(req).data.get(symbol, [])
    return bars


def mean(vals: list[float]) -> float:
    return sum(vals) / max(1, len(vals))


def make_signal(bars) -> dict | None:
    if len(bars) < 6:
        return None

    last = bars[-1]
    closes = [b.close for b in bars[-6:-1]]
    ma = mean(closes)

    price = float(last.close)
    diff = (price - ma) / ma if ma else 0.0

    # إعدادات بسيطة مثل اللي كنت تستخدمها
    min_diff = env_float("MIN_DIFF_PCT", "0.0010")   # 0.10%
    max_jump = env_float("MAX_JUMP_PCT", "0.0030")   # 0.30%

    # منع إشارات إذا الحركة الأخيرة كبيرة (فوات/قفزة)
    recent_move = 0.0
    if len(bars) >= 3:
        recent_move = abs(float(bars[-1].close) - float(bars[-3].close)) / float(bars[-3].close)

    if recent_move > max_jump:
        return None

    if abs(diff) < min_diff:
        return None

    side = "LONG" if diff > 0 else "SHORT"
    return {
        "side": side,
        "price": price,
        "ma": ma,
        "diff": diff,
        "recent_move": recent_move,
        "time": now_utc().strftime("%Y-%m-%d %H:%M:%S"),
    }


def place_trade(trading: TradingClient, symbol: str, side: str) -> str:
    usd_per_trade = env_float("USD_PER_TRADE", "2000")
    tif = TimeInForce.DAY

    # Market order (alerts-bot، والشراء يتم بسرعة)
    order_side = OrderSide.BUY if side == "LONG" else OrderSide.SELL
    qty = math.floor(usd_per_trade / 10)  # احتياط بسيط، ما نعتمد على سعر لحظي هنا
    qty = max(1, qty)

    req = MarketOrderRequest(
        symbol=symbol,
        qty=qty,
        side=order_side,
        time_in_force=tif,
    )
    o = trading.submit_order(req)
    return f"ORDER sent: {symbol} {side} qty={qty} id={getattr(o, 'id', 'N/A')}"


# ----------------- Main loop -----------------
def main():
    symbols = parse_symbols()
    interval = env_int("INTERVAL_SEC", "15")
    auto_trade = env_bool("AUTO_TRADE", "false")

    hist, trading, paper = build_clients()

    # ✅ لا نستخدم trading.paper نهائياً
    send_telegram(
        f"✅ Bot started (ALERTS) | symbols={','.join(symbols)} | interval={interval}s | feed=IEX | paper={paper} | auto_trade={auto_trade}"
    )

    last_sent: dict[str, float] = {}

    while True:
        try:
            for sym in symbols:
                bars = get_last_bars(hist, sym, minutes=15)
                sig = make_signal(bars)
                if not sig:
                    continue

                key = f"{sym}:{sig['side']}"
                now_ts = time.time()
                # تهدئة تكرار نفس الإشارة
                if key in last_sent and (now_ts - last_sent[key]) < 60:
                    continue
                last_sent[key] = now_ts

                msg = (
                    f"📣 Signal: {sig['side']} | {sym}\n"
                    f"Price: {sig['price']:.2f}\n"
                    f"MA(5m): {sig['ma']:.2f}\n"
                    f"Diff: {sig['diff']*100:.2f}%\n"
                    f"Recent Move(approx): {sig['recent_move']*100:.2f}%\n"
                    f"Time(UTC): {sig['time']}"
                )
                send_telegram(msg)

                if auto_trade:
                    try:
                        resp = place_trade(trading, sym, sig["side"])
                        send_telegram("🤖 " + resp)
                    except Exception as e:
                        send_telegram(f"⚠️ Trade error: {type(e).__name__}: {e}")

            time.sleep(interval)

        except Exception as e:
            # أهم شيء لا ينهار العامل بالكامل — يرسل الخطأ ويكمل
            try:
                send_telegram(f"⚠️ Bot error: {type(e).__name__}: {e}")
            except Exception:
                pass
            time.sleep(5)


if __name__ == "__main__":
    main()
