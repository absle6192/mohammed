import os
import time
import requests
from datetime import datetime, timezone, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
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


# ----------------- NEW: Early micro-confirm helpers -----------------
def bps_to_pct(bps: float) -> float:
    return bps / 10000.0


def strength_rank(vol_ratio: float) -> int:
    # 0 Weak, 1 OK, 2 Strong, 3 Strong+, 4 Very Strong
    if vol_ratio >= 3.0:
        return 4
    if vol_ratio >= 2.5:
        return 3
    if vol_ratio >= 2.0:
        return 2
    if vol_ratio >= 1.3:
        return 1
    return 0


def min_strength_rank(name: str) -> int:
    name = (name or "OK").strip().upper()
    mapping = {
        "WEAK": 0,
        "OK": 1,
        "STRONG": 2,
        "STRONG+": 3,
        "VERY_STRONG": 4,
        "VERY": 4,
    }
    return mapping.get(name, 1)


def get_mid_and_spread_pct(client: StockHistoricalDataClient, symbol: str) -> tuple[float, float]:
    q = client.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=[symbol])
    ).quotes[symbol]

    bid = float(q.bid_price)
    ask = float(q.ask_price)

    # robust mid
    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        return mid, spread_pct

    # fallback if one side missing
    px = float(q.ask_price or q.bid_price or 0.0)
    return px, 1.0


def micro_confirm_early(
    client: StockHistoricalDataClient,
    symbol: str,
    side: str,  # "LONG" or "SHORT"
    last_close_completed: float,  # price_now من الشمعة المكتملة
    early_trigger_bps: float,
    micro_confirm_sec: int,
    micro_confirm_bps: float,
    early_max_spread_pct: float,
) -> bool:
    # Snapshot 1
    mid1, sp1 = get_mid_and_spread_pct(client, symbol)
    if sp1 > early_max_spread_pct:
        return False

    trig = bps_to_pct(early_trigger_bps)
    move1 = (mid1 - last_close_completed) / last_close_completed

    # must actually move a bit in the signal direction before we confirm
    if side == "LONG":
        if move1 < trig:
            return False
    else:  # SHORT
        if move1 > -trig:
            return False

    # Wait tiny confirmation window
    time.sleep(max(1, int(micro_confirm_sec)))

    # Snapshot 2
    mid2, sp2 = get_mid_and_spread_pct(client, symbol)
    if sp2 > early_max_spread_pct:
        return False

    move2 = (mid2 - last_close_completed) / last_close_completed
    rev = bps_to_pct(micro_confirm_bps)

    # reject fast reversal against direction
    if side == "LONG":
        return move2 >= (trig - rev)
    else:
        return move2 <= (-trig + rev)


# ----------------- Candle filter -----------------
def candle_filter_light_completed(df_all, side: str, close_pos_min: float = 0.65) -> bool:
    """
    فلتر شموع خفيف لكن على شموع مكتملة:
    - نستخدم آخر شمعة مكتملة = -2
    - والشمعة اللي قبلها = -3

    LONG:
      - الشمعة (-2) خضراء
      - إغلاقها أعلى من إغلاق (-3)
      - إغلاقها قريب من أعلى الشمعة
    SHORT:
      - الشمعة (-2) حمراء
      - إغلاقها أقل من إغلاق (-3)
      - إغلاقها قريب من أسفل الشمعة
    """
    if df_all is None or len(df_all) < 4:
        return False

    last = df_all.iloc[-2]  # completed candle
    prev = df_all.iloc[-3]  # completed candle before it

    o = float(last["open"])
    h = float(last["high"])
    l = float(last["low"])
    c = float(last["close"])
    prev_c = float(prev["close"])

    rng = h - l
    if rng <= 0:
        return False

    close_pos = (c - l) / rng  # 0 عند اللو, 1 عند الهاي

    if side == "LONG":
        return (c >= o) and (c > prev_c) and (close_pos >= close_pos_min)

    # SHORT
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

🔥 حجم التداول | Volume Spike (baseline):
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

    mode = env("MODE", "EARLY").upper()
    if mode not in ("EARLY", "CONFIRM", "BOTH"):
        mode = "EARLY"

    interval_sec = int(env("INTERVAL_SEC", "15" if mode in ("EARLY", "BOTH") else "20"))
    lookback_min = int(env("LOOKBACK_MIN", "3" if mode in ("EARLY", "BOTH") else "5"))

    thresh_pct = float(env("THRESH_PCT", "0.0008" if mode in ("EARLY", "BOTH") else "0.0015"))

    volume_mult = float(env("VOLUME_MULT", "1.2" if mode in ("EARLY", "BOTH") else "1.8"))
    min_vol_ratio = float(env("MIN_VOL_RATIO", "1.1" if mode in ("EARLY", "BOTH") else "1.5"))

    cooldown_min = int(env("COOLDOWN_MIN", "6" if mode in ("EARLY", "BOTH") else "10"))

    recent_window_min = int(env("RECENT_WINDOW_MIN", "10"))
    max_recent_move_pct = float(env("MAX_RECENT_MOVE_PCT", "0.003"))

    candle_filter_mode = env("CANDLE_FILTER", "LIGHT").upper()  # LIGHT / OFF
    candle_close_pos_min = float(env("CANDLE_CLOSE_POS_MIN", "0.65"))

    # ----------------- NEW: read EARLY micro-confirm env -----------------
    early_mode_on = env("EARLY_MODE", "OFF").strip().upper() in ("ON", "TRUE", "1", "YES")
    early_trigger_bps = float(env("EARLY_TRIGGER_BPS", "8"))
    micro_confirm_sec = int(env("MICRO_CONFIRM_SEC", "3"))
    micro_confirm_bps = float(env("MICRO_CONFIRM_BPS", "4"))
    early_max_spread_pct = float(env("EARLY_MAX_SPREAD_PCT", env("MAX_SPREAD_PCT", "0.004")))
    early_min_strength = min_strength_rank(env("EARLY_MIN_STRENGTH", "OK"))

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
        f"⚡ Early MicroConfirm: {'ON' if early_mode_on else 'OFF'} "
        f"| Trigger: {early_trigger_bps}bps | Confirm: {micro_confirm_sec}s/{micro_confirm_bps}bps "
        f"| SpreadMax: {early_max_spread_pct} | MinStrength: {env('EARLY_MIN_STRENGTH','OK')}\n"
        f"🕒 Timezone: UTC\n"
        f"🕯️ Using COMPLETED candles (-2/-3)"
    )

    while True:
        try:
            now = datetime.now(timezone.utc)

            need_min = max(lookback_min, recent_window_min) + 6
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
                if len(df_all) < max(8, lookback_min + 4):
                    continue

                # ===== completed "now" =====
                price_now = float(df_all["close"].iloc[-2])  # completed candle close

                # ===== late-entry filter (completed candles) =====
                df_recent = df_all.tail(recent_window_min + 2)
                if len(df_recent) < 4:
                    continue

                price_then = float(df_recent["close"].iloc[0])
                recent_move = pct(price_now, price_then)

                if abs(recent_move) > max_recent_move_pct:
                    continue

                # ===== lookback MA (completed candles) =====
                df_lb = df_all.tail(lookback_min + 2).copy()
                if len(df_lb) < (lookback_min + 2):
                    continue

                ma = float(df_lb["close"].iloc[-(lookback_min + 1):-1].mean())
                d = pct(price_now, ma)

                # ===== volume baseline (completed candle) =====
                vol_last = float(df_lb["volume"].iloc[-2])  # completed candle volume
                vol_base = (
                    float(df_lb["volume"].iloc[:-2].mean())
                    if len(df_lb) > 4
                    else float(df_lb["volume"].mean())
                )
                vol_ratio = (vol_last / vol_base) if vol_base else 0.0

                vol_ok = (vol_base > 0) and (vol_last >= vol_base * volume_mult) and (vol_ratio >= min_vol_ratio)
                if not vol_ok:
                    continue

                signals_to_send: list[tuple[str, str]] = []

                if mode in ("EARLY", "BOTH"):
                    if d >= thresh_pct:
                        signals_to_send.append(("🟡 EARLY", "LONG"))
                    elif d <= -thresh_pct:
                        signals_to_send.append(("🟡 EARLY", "SHORT"))

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
                    candle_ok = True
                    if candle_filter_mode != "OFF" and "EARLY" in mode_tag:
                        candle_ok = candle_filter_light_completed(df_all, side, close_pos_min=candle_close_pos_min)
                        if not candle_ok:
                            continue

                    # ----------------- NEW: micro-confirm for EARLY -----------------
                    if early_mode_on and "EARLY" in mode_tag:
                        # apply only if strength >= minimum
                        if strength_rank(vol_ratio) >= early_min_strength:
                            ok = micro_confirm_early(
                                client=client,
                                symbol=sym,
                                side=side,
                                last_close_completed=price_now,
                                early_trigger_bps=early_trigger_bps,
                                micro_confirm_sec=micro_confirm_sec,
                                micro_confirm_bps=micro_confirm_bps,
                                early_max_spread_pct=early_max_spread_pct,
                            )
                            if not ok:
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
