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
        "disable_notification": False,
    }

    r = requests.post(url, json=payload, timeout=15)
    if not r.ok:
        raise RuntimeError(f"Telegram error: {r.status_code} {r.text}")


def pct(a: float, b: float) -> float:
    if b == 0:
        return 0.0
    return (a - b) / b


def fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def strength_label(vol_ratio: float) -> str:
    if vol_ratio >= 3.0:
        return "🔥🔥🔥 نار (Very Strong)"
    if vol_ratio >= 2.5:
        return "🔥🔥 قوية جدًا (Strong+)"
    if vol_ratio >= 2.0:
        return "🔥 قوية (Strong)"
    if vol_ratio >= 1.3:
        return "✅ متوسطة (OK)"
    return "⚠️ ضعيفة (Weak)"


def candle_filter_light(df_all, side: str, close_pos_min: float = 0.65) -> bool:
    """
    فلتر شموع خفيف (بدون ما تحتاج تفهم شموع):
    - LONG: آخر شمعة خضراء + close أعلى من prev close + close قريب من high
    - SHORT: آخر شمعة حمراء + close أقل من prev close + close قريب من low
    """
    if df_all is None or len(df_all) < 2:
        return False

    last = df_all.iloc[-1]
    prev = df_all.iloc[-2]

    o = float(last["open"])
    h = float(last["high"])
    l = float(last["low"])
    c = float(last["close"])
    prev_c = float(prev["close"])

    rng = h - l
    if rng <= 0:
        return False

    # مكان الإغلاق داخل الشمعة: 0 = عند اللو, 1 = عند الهاي
    close_pos = (c - l) / rng

    if side == "LONG":
        # شمعة خضراء + زخم بسيط (أعلى من إغلاق الشمعة السابقة) + إغلاق قريب من الأعلى
        return (c >= o) and (c > prev_c) and (close_pos >= close_pos_min)

    # SHORT
    # شمعة حمراء + زخم بسيط (أقل من إغلاق الشمعة السابقة) + إغلاق قريب من الأسفل
    return (c <= o) and (c < prev_c) and (close_pos <= (1.0 - close_pos_min))


def build_message(
    mode_tag: str,
    side: str,
    symbol: str,
    price_now: float,
    ma: float,
    d: float,
    vol_last: float,
    vol_base: float,
    vol_ratio: float,
    lookback_min: int,
    now: datetime,
    recent_move: float,
    recent_window_min: int,
    candle_ok: bool,
) -> str:
    if side == "LONG":
        direction_emoji = "🟢📈"
        direction_ar = "شراء"
        bias_emoji = "🚀"
    else:
        direction_emoji = "🔴📉"
        direction_ar = "بيع (شورت)"
        bias_emoji = "🧨"

    diff_str = fmt_pct(d)
    diff_arrow = "⬆️" if d > 0 else "⬇️" if d < 0 else "➡️"
    strength = strength_label(vol_ratio)
    ts = now.strftime("%Y-%m-%d %H:%M:%S")

    candle_str = "✅ PASS" if candle_ok else "❌ FAIL"

    msg = f"""
{direction_emoji} {mode_tag} | إشارة {direction_ar} | {side} {bias_emoji}
📌 السهم | Symbol: {symbol}

💰 السعر | Price: {price_now:.2f}
📊 المتوسط ({lookback_min}د) | MA({lookback_min}m): {ma:.2f}

{diff_arrow} الفرق | Diff: {diff_str}

🔥 حجم التداول | Volume Spike (early baseline):
{vol_last:.0f} مقابل {vol_base:.0f} (x{vol_ratio:.2f})

🧠 حركة {recent_window_min}د الأخيرة | Recent Move:
{fmt_pct(recent_move)}

🕯️ Candle Filter (LIGHT):
{candle_str}

⭐️ قوة الإشارة | Strength:
{strength}

⏰ الوقت | Time (UTC):
{ts}
""".strip()

    return msg


def main():
    _base_url = env("APCA_API_BASE_URL")
    key_id = env("APCA_API_KEY_ID")
    secret = env("APCA_API_SECRET_KEY")

    tickers = [t.strip().upper() for t in env("TICKERS").split(",") if t.strip()]

    # ===== وضع الإشارات =====
    # EARLY  = بداية الحركة
    # CONFIRM= تأكيد الحركة (اختياري)
    # BOTH   = يرسل الاثنين
    mode = env("MODE", "EARLY").upper()
    if mode not in ("EARLY", "CONFIRM", "BOTH"):
        mode = "EARLY"

    # ===== إعدادات افتراضية للوضع المبكر =====
    interval_sec = int(env("INTERVAL_SEC", "15" if mode in ("EARLY", "BOTH") else "20"))
    lookback_min = int(env("LOOKBACK_MIN", "3" if mode in ("EARLY", "BOTH") else "5"))

    # Threshold: 0.0008 = 0.08%
    thresh_pct = float(env("THRESH_PCT", "0.0008" if mode in ("EARLY", "BOTH") else "0.0015"))

    # حجم التداول: وضع EARLY لا ينتظر انفجار قوي
    volume_mult = float(env("VOLUME_MULT", "1.2" if mode in ("EARLY", "BOTH") else "1.8"))
    min_vol_ratio = float(env("MIN_VOL_RATIO", "1.1" if mode in ("EARLY", "BOTH") else "1.5"))

    cooldown_min = int(env("COOLDOWN_MIN", "6" if mode in ("EARLY", "BOTH") else "10"))

    # ===== فلتر منع الدخول المتأخر =====
    recent_window_min = int(env("RECENT_WINDOW_MIN", "10"))
    max_recent_move_pct = float(env("MAX_RECENT_MOVE_PCT", "0.003"))  # 0.30%

    # ===== فلتر الشموع الخفيف =====
    # تقدر تقفله: CANDLE_FILTER=OFF
    candle_filter_mode = env("CANDLE_FILTER", "LIGHT").upper()  # LIGHT / OFF
    candle_close_pos_min = float(env("CANDLE_CLOSE_POS_MIN", "0.65"))  # كل ما زاد صار أقوى (0.6-0.75)

    client = StockHistoricalDataClient(key_id, secret)

    last_signal_time: dict[str, datetime] = {}
    last_signal_key: dict[str, str] = {}

    send_telegram(
        "✅ البوت اشتغل | Bot Started\n"
        f"👀 يراقب | Watching: {', '.join(tickers)}\n"
        f"⚙️ MODE: {mode}\n"
        f"⏱️ Interval: {interval_sec}s | Lookback: {lookback_min}m\n"
        f"🎯 Threshold: {thresh_pct*100:.2f}%\n"
        f"🔥 Volume Mult: x{volume_mult} | Min Vol Ratio: x{min_vol_ratio}\n"
        f"🧠 Late-Entry Filter: abs(move {recent_window_min}m) <= {max_recent_move_pct*100:.2f}%\n"
        f"🕯️ Candle Filter: {candle_filter_mode} | ClosePosMin: {candle_close_pos_min}\n"
        f"🕒 Timezone: UTC"
    )

    while True:
        try:
            now = datetime.now(timezone.utc)

            need_min = max(lookback_min, recent_window_min) + 3
            start = now - timedelta(minutes=need_min)

            req = StockBarsRequest(
                symbol_or_symbols=tickers,
                timeframe=TimeFrame.Minute,
                start=start,
                end=now,
                feed="iex",
            )

            bars = client.get_stock_bars(req).df
            if bars is None or len(bars) == 0:
                time.sleep(interval_sec)
                continue

            for sym in tickers:
                try:
                    df_all = bars.xs(sym, level=0).copy()
                except Exception:
                    continue

                df_all = df_all.sort_index()
                if len(df_all) < max(6, lookback_min + 2):
                    continue

                # ===== فلتر late-entry =====
                df_recent = df_all.tail(recent_window_min)
                if len(df_recent) < 3:
                    continue

                price_now = float(df_recent["close"].iloc[-1])
                price_then = float(df_recent["close"].iloc[0])
                recent_move = pct(price_now, price_then)

                if abs(recent_move) > max_recent_move_pct:
                    continue

                # ===== lookback =====
                df = df_all.tail(lookback_min).copy()
                if len(df) < 3:
                    continue

                ma = float(df["close"].mean())
                d = pct(price_now, ma)

                # ===== Baseline حجم مبكر =====
                vol_last = float(df["volume"].iloc[-1])
                vol_base = float(df["volume"].iloc[:-1].mean()) if len(df) > 2 else float(df["volume"].mean())
                vol_ratio = (vol_last / vol_base) if vol_base else 0.0

                vol_ok = (vol_base > 0) and (vol_last >= vol_base * volume_mult) and (vol_ratio >= min_vol_ratio)

                # ===== إشارات =====
                signals_to_send: list[tuple[str, str]] = []

                if vol_ok:
                    # EARLY / BOTH
                    if mode in ("EARLY", "BOTH"):
                        if d >= thresh_pct:
                            signals_to_send.append(("🟡 EARLY", "LONG"))
                        elif d <= -thresh_pct:
                            signals_to_send.append(("🟡 EARLY", "SHORT"))

                    # CONFIRM / BOTH (اختياري)
                    if mode in ("CONFIRM", "BOTH"):
                        confirm_thresh = float(env("CONFIRM_THRESH_PCT", str(max(thresh_pct * 1.8, 0.0015))))
                        confirm_vol_mult = float(env("CONFIRM_VOLUME_MULT", str(max(volume_mult * 1.4, 1.8))))
                        confirm_ok = (vol_last >= vol_base * confirm_vol_mult)

                        if confirm_ok:
                            if d >= confirm_thresh:
                                signals_to_send.append(("🟢 CONFIRM", "LONG"))
                            elif d <= -confirm_thresh:
                                signals_to_send.append(("🟢 CONFIRM", "SHORT"))

                if not signals_to_send:
                    continue

                for mode_tag, side in signals_to_send:
                    # ===== فلتر الشموع الخفيف: نطبقه على EARLY فقط (عشان يقلل الفيك بريك) =====
                    candle_ok = True
                    if candle_filter_mode != "OFF" and "EARLY" in mode_tag:
                        candle_ok = candle_filter_light(df_all, side, close_pos_min=candle_close_pos_min)
                        if not candle_ok:
                            continue

                    key = f"{mode_tag}_{side}"

                    last_t = last_signal_time.get(sym)
                    if last_t and (now - last_t) < timedelta(minutes=cooldown_min):
                        continue

                    if last_signal_key.get(sym) == key and last_t and (now - last_t) < timedelta(minutes=cooldown_min * 2):
                        continue

                    msg = build_message(
                        mode_tag=mode_tag,
                        side=side,
                        symbol=sym,
                        price_now=price_now,
                        ma=ma,
                        d=d,
                        vol_last=vol_last,
                        vol_base=vol_base,
                        vol_ratio=vol_ratio,
                        lookback_min=lookback_min,
                        now=now,
                        recent_move=recent_move,
                        recent_window_min=recent_window_min,
                        candle_ok=candle_ok,
                    )

                    send_telegram(msg)
                    last_signal_time[sym] = now
                    last_signal_key[sym] = key

        except Exception as e:
            try:
                send_telegram(f"⚠️ خطأ | Bot error: {type(e).__name__}: {e}")
            except Exception:
                pass

        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
