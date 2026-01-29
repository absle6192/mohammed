import os
import time
import requests
from datetime import datetime, timezone, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame


def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def send_telegram(text: str) -> None:
    token = env("TELEGRAM_BOT_TOKEN")
    chat_id = env("TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": True,
        # مهم: نخليه False عشان يطلع إشعار طبيعي (النغمة تتحكم فيها من تطبيق تيليجرام)
        "disable_notification": False,
    }

    r = requests.post(url, json=payload, timeout=15)
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text}")


def pct(a: float, b: float) -> float:
    # (a-b)/b
    if b == 0:
        return 0.0
    return (a - b) / b


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def strength_label(vol_ratio: float) -> str:
    # قوة الإشارة حسب نسبة حجم التداول
    if vol_ratio >= 3.0:
        return "🔥🔥🔥 نار (Very Strong)"
    if vol_ratio >= 2.5:
        return "🔥🔥 قوية جدًا (Strong+)"
    if vol_ratio >= 2.0:
        return "🔥 قوية (Strong)"
    if vol_ratio >= 1.5:
        return "✅ متوسطة (OK)"
    return "⚠️ ضعيفة (Weak)"


def build_message(
    side: str,
    symbol: str,
    price_now: float,
    ma: float,
    d: float,
    vol_last: float,
    vol_avg: float,
    vol_ratio: float,
    lookback_min: int,
    now: datetime,
) -> str:
    # اتجاه + إيموجي
    if side == "LONG":
        direction_emoji = "🟢📈"
        direction_ar = "شراء"
        bias_emoji = "🚀"
    else:
        direction_emoji = "🔴📉"
        direction_ar = "بيع (شورت)"
        bias_emoji = "🧨"

    # فرق السعر (نعرضه مع إشارة + أو -)
    diff_str = fmt_pct(d)
    diff_arrow = "⬆️" if d > 0 else "⬇️" if d < 0 else "➡️"

    strength = strength_label(vol_ratio)

    # تنسيق وقت UTC
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    # رسالة عربي + إنجليزي + إيموجي ذكي
    msg = f"""
{direction_emoji} إشارة {direction_ar} | {side} {bias_emoji}
📌 السهم | Symbol: {symbol}

💰 السعر | Price: {price_now:.2f}
📊 المتوسط ({lookback_min}د) | MA({lookback_min}m): {ma:.2f}

{diff_arrow} الفرق | Diff: {diff_str}

🔥 حجم التداول | Volume Spike:
{vol_last:.0f} مقابل {vol_avg:.0f} (x{vol_ratio:.2f})

⭐️ قوة الإشارة | Strength:
{strength}

⏰ الوقت | Time (UTC):
{ts}
""".strip()

    return msg


def main():
    # موجود عندك سابقًا (حتى لو ما نستخدمه هنا)
    _base_url = env("APCA_API_BASE_URL")
    key_id = env("APCA_API_KEY_ID")
    secret = env("APCA_API_SECRET_KEY")

    tickers = [t.strip().upper() for t in env("TICKERS").split(",") if t.strip()]

    interval_sec = int(env("INTERVAL_SEC", "20"))
    lookback_min = int(env("LOOKBACK_MIN", "5"))

    # 0.003 = 0.30%
    thresh_pct = float(env("THRESH_PCT", "0.003"))

    # لازم يكون فيه spike: vol_last >= vol_avg * VOLUME_MULT
    volume_mult = float(env("VOLUME_MULT", "1.8"))

    cooldown_min = int(env("COOLDOWN_MIN", "10"))

    # فلتر إضافي (اختياري): إذا كانت نسبة الحجم أقل من هذا، ما نرسل إشعار
    # مثال: 1.5 يعني لازم vol_last >= 1.5 * vol_avg على الأقل
    min_vol_ratio = float(env("MIN_VOL_RATIO", "1.5"))

    client = StockHistoricalDataClient(key_id, secret)

    # cooldown memory
    last_signal_time: dict[str, datetime] = {}
    last_signal_side: dict[str, str] = {}  # "LONG" / "SHORT"

    send_telegram(
        "✅ البوت اشتغل | Bot Started\n"
        f"👀 يراقب | Watching: {', '.join(tickers)}\n"
        f"⏱️ Interval: {interval_sec}s | Lookback: {lookback_min}m\n"
        f"🎯 Threshold: {thresh_pct*100:.2f}% | Volume Mult: x{volume_mult}\n"
        f"🧹 Min Vol Ratio (filter): x{min_vol_ratio}\n"
        f"🕒 Timezone: UTC"
    )

    while True:
        try:
            now = datetime.now(timezone.utc)
            start = now - timedelta(minutes=lookback_min + 2)  # buffer

            req = StockBarsRequest(
                symbol_or_symbols=tickers,
                timeframe=TimeFrame.Minute,
                start=start,
                end=now,
                feed="iex",  # if you have SIP you can change
            )

            bars = client.get_stock_bars(req).df  # MultiIndex: (symbol, timestamp)
            if bars is None or len(bars) == 0:
                time.sleep(interval_sec)
                continue

            for sym in tickers:
                try:
                    df = bars.xs(sym, level=0).copy()
                except Exception:
                    continue

                # keep only last N minutes
                df = df.sort_index().tail(lookback_min)

                if len(df) < max(3, lookback_min - 1):
                    continue

                price_now = float(df["close"].iloc[-1])
                ma = float(df["close"].mean())

                vol_last = float(df["volume"].iloc[-1])
                vol_avg = float(df["volume"].mean())
                vol_ratio = (vol_last / vol_avg) if vol_avg else 0.0

                # شرط حجم تداول قوي (القديم) + فلتر ratio (الجديد)
                vol_ok = (vol_avg > 0) and (vol_last >= vol_avg * volume_mult) and (vol_ratio >= min_vol_ratio)

                d = pct(price_now, ma)

                side = None
                if d >= thresh_pct and vol_ok:
                    side = "LONG"
                elif d <= -thresh_pct and vol_ok:
                    side = "SHORT"

                if side is None:
                    continue

                # cooldown
                last_t = last_signal_time.get(sym)
                if last_t and (now - last_t) < timedelta(minutes=cooldown_min):
                    continue

                # avoid repeating same side too often
                if (
                    last_signal_side.get(sym) == side
                    and last_t
                    and (now - last_t) < timedelta(minutes=cooldown_min * 2)
                ):
                    continue

                msg = build_message(
                    side=side,
                    symbol=sym,
                    price_now=price_now,
                    ma=ma,
                    d=d,
                    vol_last=vol_last,
                    vol_avg=vol_avg,
                    vol_ratio=vol_ratio,
                    lookback_min=lookback_min,
                    now=now,
                )

                send_telegram(msg)

                last_signal_time[sym] = now
                last_signal_side[sym] = side

        except Exception as e:
            # يرسل خطأ مختصر (مع محاولة عدم كسر البوت)
            try:
                send_telegram(f"⚠️ خطأ | Bot error: {type(e).__name__}: {e}")
            except Exception:
                pass

        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
