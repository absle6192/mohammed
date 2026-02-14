import os
import time
import json
import threading
import requests
from datetime import datetime, timezone, timedelta

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame


# ===================== env helpers =====================
def env(name: str, default: str | None = None) -> str:
    v = os.getenv(name, default)
    if v is None or str(v).strip() == "":
        raise RuntimeError(f"Missing env var: {name}")
    return str(v).strip()


def env_int(name: str, default: str) -> int:
    return int(env(name, default))


def env_float(name: str, default: str) -> float:
    return float(env(name, default))


def env_bool(name: str, default: str = "false") -> bool:
    v = env(name, default).strip().lower()
    return v in ("1", "true", "on", "yes", "y")


# ===================== Telegram helpers =====================
class TelegramAPI:
    def __init__(self, token: str, chat_id: str):
        self.token = token
        self.chat_id = chat_id
        self.base = f"https://api.telegram.org/bot{token}"

    def send_message(self, text: str, reply_markup: dict | None = None) -> None:
        url = f"{self.base}/sendMessage"
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "disable_notification": False,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        r = requests.post(url, json=payload, timeout=20)
        if not r.ok:
            raise RuntimeError(f"Telegram sendMessage error: {r.status_code} {r.text}")

    def edit_message_text(self, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        url = f"{self.base}/editMessageText"
        payload = {
            "chat_id": self.chat_id,
            "message_id": message_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        r = requests.post(url, json=payload, timeout=20)
        if not r.ok:
            # editing sometimes fails if message old; ignore softly
            pass

    def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
        url = f"{self.base}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        requests.post(url, json=payload, timeout=20)

    def get_updates(self, offset: int, timeout_sec: int = 25) -> dict:
        url = f"{self.base}/getUpdates"
        payload = {
            "offset": offset,
            "timeout": timeout_sec,
            "allowed_updates": ["callback_query", "message"],
        }
        r = requests.post(url, json=payload, timeout=timeout_sec + 10)
        if not r.ok:
            return {"ok": False, "result": []}
        return r.json()


# ===================== math helpers =====================
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


# ----------------- Early micro-confirm helpers -----------------
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


# ✅ FIXED: supports dict OR object response across Alpaca versions
def get_mid_and_spread_pct(client: StockHistoricalDataClient, symbol: str) -> tuple[float, float]:
    resp = client.get_stock_latest_quote(
        StockLatestQuoteRequest(symbol_or_symbols=[symbol])
    )

    # Some SDK versions return dict { "SYM": Quote }
    if isinstance(resp, dict):
        q = resp.get(symbol)
    else:
        # Others return object with .quotes dict
        quotes = getattr(resp, "quotes", None)
        q = quotes.get(symbol) if isinstance(quotes, dict) else None

    if q is None:
        return 0.0, 1.0

    bid = float(getattr(q, "bid_price", 0.0) or 0.0)
    ask = float(getattr(q, "ask_price", 0.0) or 0.0)

    if bid > 0 and ask > 0:
        mid = (bid + ask) / 2.0
        spread_pct = (ask - bid) / mid if mid > 0 else 1.0
        return mid, spread_pct

    px = float((getattr(q, "ask_price", 0.0) or getattr(q, "bid_price", 0.0) or 0.0))
    return px, 1.0


def micro_confirm_early(
    client: StockHistoricalDataClient,
    symbol: str,
    side: str,  # "LONG" or "SHORT"
    last_close_completed: float,
    early_trigger_bps: float,
    micro_confirm_sec: int,
    micro_confirm_bps: float,
    early_max_spread_pct: float,
) -> bool:
    mid1, sp1 = get_mid_and_spread_pct(client, symbol)
    if sp1 > early_max_spread_pct:
        return False

    if last_close_completed <= 0:
        return False

    trig = bps_to_pct(early_trigger_bps)
    move1 = (mid1 - last_close_completed) / last_close_completed

    if side == "LONG":
        if move1 < trig:
            return False
    else:  # SHORT
        if move1 > -trig:
            return False

    time.sleep(max(1, int(micro_confirm_sec)))

    mid2, sp2 = get_mid_and_spread_pct(client, symbol)
    if sp2 > early_max_spread_pct:
        return False

    move2 = (mid2 - last_close_completed) / last_close_completed
    rev = bps_to_pct(micro_confirm_bps)

    if side == "LONG":
        return move2 >= (trig - rev)
    else:
        return move2 <= (-trig + rev)


# ----------------- Candle filter -----------------
def candle_filter_light_completed(df_all, side: str, close_pos_min: float = 0.65) -> bool:
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

💰 السعر (شمعة مكتملة) | Price: {price_now:.2f}
📊 المتوسط ({lookback_min}د) | MA({lookback_min}m): {ma:.2f}

{diff_arrow} الفرق | Diff: {diff_str}

🔥 حجم التداول | Volume Spike:
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


# ===================== NEW: Click-to-confirm state =====================
state_lock = threading.Lock()

# tracks a user's "I entered" for a symbol
active_entries: dict[str, dict] = {}  # sym -> {side, entry_px, ts, source_msg_id}

# tracks last EARLY message id (so we can edit it / mark handled)
last_early_msg_ref: dict[str, dict] = {}  # sym -> {message_id, side, price_hint, ts}

# background polling offset
telegram_offset = 0


def build_entry_keyboard(symbol: str, suggested_side: str) -> dict:
    # always offer both, plus ignore
    # callback format: ENTER|SIDE|SYM
    buy_btn = {"text": "✅ دخلت شراء", "callback_data": f"ENTER|LONG|{symbol}"}
    sh_btn = {"text": "✅ دخلت شورت", "callback_data": f"ENTER|SHORT|{symbol}"}
    ig_btn = {"text": "❌ تجاهل", "callback_data": f"IGNORE|{symbol}"}

    # put suggested first
    if suggested_side == "LONG":
        rows = [[buy_btn, sh_btn], [ig_btn]]
    else:
        rows = [[sh_btn, buy_btn], [ig_btn]]

    return {"inline_keyboard": rows}


def build_clear_keyboard() -> dict:
    return {"inline_keyboard": []}


def confirm_monitor(
    client: StockHistoricalDataClient,
    tg: TelegramAPI,
    symbol: str,
    side: str,
    entry_px: float,
    confirm_window_sec: int,
    confirm_trigger_bps: float,
    confirm_max_pullback_bps: float,
    confirm_poll_sec: int,
    confirm_max_spread_pct: float,
):
    """
    After user clicks "entered", monitor quotes briefly.
    LONG confirm:
      - must go >= entry*(1 + trigger)
      - must not pull back below entry*(1 - max_pullback) before confirming (optional safety)
    SHORT confirm:
      - must go <= entry*(1 - trigger)
      - must not bounce above entry*(1 + max_pullback) before confirming
    """
    trig = bps_to_pct(confirm_trigger_bps)
    pb = bps_to_pct(confirm_max_pullback_bps)

    start = time.time()
    best_move = 0.0

    while time.time() - start <= confirm_window_sec:
        mid, sp = get_mid_and_spread_pct(client, symbol)
        if mid <= 0:
            time.sleep(confirm_poll_sec)
            continue

        if sp > confirm_max_spread_pct:
            # ignore illiquid moments
            time.sleep(confirm_poll_sec)
            continue

        move = (mid - entry_px) / entry_px
        if side == "LONG":
            best_move = max(best_move, move)

            # fail-fast pullback (optional)
            if move <= -pb:
                tg.send_message(
                    f"⚠️ NO CONFIRM (Pullback سريع)\n"
                    f"📌 {symbol} | LONG\n"
                    f"Entry ~ {entry_px:.2f} | Now {mid:.2f} ({fmt_pct(move)})\n"
                    f"🧠 ما تأكد الزخم خلال نافذة المتابعة."
                )
                return

            if move >= trig:
                tg.send_message(
                    f"🔥 MOMENTUM CONFIRMED ✅\n"
                    f"📌 {symbol} | LONG\n"
                    f"Entry ~ {entry_px:.2f} | Now {mid:.2f} ({fmt_pct(move)})\n"
                    f"🚀 الزخم مستمر — تقدر تستهدف 15–25$ حسب خطتك."
                )
                return
        else:
            best_move = min(best_move, move)

            if move >= pb:
                tg.send_message(
                    f"⚠️ NO CONFIRM (Bounce سريع)\n"
                    f"📌 {symbol} | SHORT\n"
                    f"Entry ~ {entry_px:.2f} | Now {mid:.2f} ({fmt_pct(move)})\n"
                    f"🧠 ما تأكد الزخم خلال نافذة المتابعة."
                )
                return

            if move <= -trig:
                tg.send_message(
                    f"🔥 MOMENTUM CONFIRMED ✅\n"
                    f"📌 {symbol} | SHORT\n"
                    f"Entry ~ {entry_px:.2f} | Now {mid:.2f} ({fmt_pct(move)})\n"
                    f"🧨 النزول مستمر — تقدر تطلع على 15–25$ حسب خطتك."
                )
                return

        time.sleep(confirm_poll_sec)

    # time out
    tg.send_message(
        f"⚠️ NO CONFIRM (Timeout)\n"
        f"📌 {symbol} | {side}\n"
        f"Entry ~ {entry_px:.2f}\n"
        f"⏱ ما جاء تأكيد خلال {confirm_window_sec}s."
    )


def telegram_poll_loop(
    client: StockHistoricalDataClient,
    tg: TelegramAPI,
    confirm_window_sec: int,
    confirm_trigger_bps: float,
    confirm_max_pullback_bps: float,
    confirm_poll_sec: int,
    confirm_max_spread_pct: float,
):
    global telegram_offset

    # Initialize offset safely: read once
    try:
        first = tg.get_updates(offset=0, timeout_sec=1)
        if isinstance(first, dict) and first.get("ok") and first.get("result"):
            telegram_offset = max(int(u["update_id"]) for u in first["result"]) + 1
        else:
            telegram_offset = 0
    except Exception:
        telegram_offset = 0

    while True:
        try:
            data = tg.get_updates(offset=telegram_offset, timeout_sec=25)
            if not isinstance(data, dict) or not data.get("ok"):
                continue

            updates = data.get("result", []) or []
            if not updates:
                continue

            for u in updates:
                telegram_offset = max(telegram_offset, int(u.get("update_id", 0)) + 1)

                cq = u.get("callback_query")
                if not cq:
                    continue

                cq_id = cq.get("id", "")
                msg = cq.get("message") or {}
                msg_id = int(msg.get("message_id", 0))
                chat = (msg.get("chat") or {})
                chat_id = str(chat.get("id", ""))

                # enforce same chat
                if chat_id and chat_id != str(tg.chat_id):
                    tg.answer_callback_query(cq_id, "غير مصرح", show_alert=True)
                    continue

                data_str = (cq.get("data") or "").strip()
                if not data_str:
                    tg.answer_callback_query(cq_id, "بيانات غير صالحة", show_alert=True)
                    continue

                parts = data_str.split("|")
                action = parts[0].upper()

                if action == "IGNORE" and len(parts) >= 2:
                    sym = parts[1].upper()
                    tg.answer_callback_query(cq_id, f"تم تجاهل {sym}")
                    # optionally clear buttons
                    tg.edit_message_text(msg_id, (msg.get("text") or "") + "\n\n🟦 (تم تجاهل الإشارة)", build_clear_keyboard())
                    with state_lock:
                        # clean pending early
                        if sym in last_early_msg_ref:
                            last_early_msg_ref.pop(sym, None)
                    continue

                if action == "ENTER" and len(parts) >= 3:
                    side = parts[1].upper()
                    sym = parts[2].upper()
                    if side not in ("LONG", "SHORT"):
                        tg.answer_callback_query(cq_id, "SIDE غير صحيح", show_alert=True)
                        continue

                    tg.answer_callback_query(cq_id, f"تم تسجيل دخولك: {sym} {side}")

                    # fetch entry price at click moment
                    mid, sp = get_mid_and_spread_pct(client, sym)
                    entry_px = mid if mid > 0 else 0.0

                    with state_lock:
                        active_entries[sym] = {
                            "side": side,
                            "entry_px": entry_px,
                            "ts": datetime.now(timezone.utc),
                            "source_msg_id": msg_id,
                        }

                    # mark message
                    suffix = "\n\n🟩 (تم تسجيل الدخول — جاري متابعة التأكيد...)"
                    tg.edit_message_text(msg_id, (msg.get("text") or "") + suffix, build_clear_keyboard())

                    # start monitor thread
                    t = threading.Thread(
                        target=confirm_monitor,
                        daemon=True,
                        args=(
                            client,
                            tg,
                            sym,
                            side,
                            entry_px,
                            confirm_window_sec,
                            confirm_trigger_bps,
                            confirm_max_pullback_bps,
                            confirm_poll_sec,
                            confirm_max_spread_pct,
                        ),
                    )
                    t.start()
                    continue

                tg.answer_callback_query(cq_id, "أمر غير معروف", show_alert=True)

        except Exception:
            # never kill loop
            time.sleep(1)


def main():
    _base_url = env("APCA_API_BASE_URL")  # not used by data client but kept for compatibility
    key_id = env("APCA_API_KEY_ID")
    secret = env("APCA_API_SECRET_KEY")

    tickers = [t.strip().upper() for t in env("TICKERS").split(",") if t.strip()]

    mode = env("MODE", "EARLY").upper()
    if mode not in ("EARLY", "CONFIRM", "BOTH"):
        mode = "EARLY"

    interval_sec = env_int("INTERVAL_SEC", "15" if mode in ("EARLY", "BOTH") else "20")
    lookback_min = env_int("LOOKBACK_MIN", "3" if mode in ("EARLY", "BOTH") else "5")

    thresh_pct = env_float("THRESH_PCT", "0.0008" if mode in ("EARLY", "BOTH") else "0.0015")

    volume_mult = env_float("VOLUME_MULT", "1.2" if mode in ("EARLY", "BOTH") else "1.8")
    min_vol_ratio = env_float("MIN_VOL_RATIO", "1.1" if mode in ("EARLY", "BOTH") else "1.5")

    cooldown_min = env_int("COOLDOWN_MIN", "6" if mode in ("EARLY", "BOTH") else "10")

    recent_window_min = env_int("RECENT_WINDOW_MIN", "10")
    max_recent_move_pct = env_float("MAX_RECENT_MOVE_PCT", "0.003")

    candle_filter_mode = env("CANDLE_FILTER", "LIGHT").upper()  # LIGHT / OFF
    candle_close_pos_min = env_float("CANDLE_CLOSE_POS_MIN", "0.65")

    # Early micro-confirm
    early_mode_on = env_bool("EARLY_MODE", "OFF")
    early_trigger_bps = env_float("EARLY_TRIGGER_BPS", "8")
    micro_confirm_sec = env_int("MICRO_CONFIRM_SEC", "3")
    micro_confirm_bps = env_float("MICRO_CONFIRM_BPS", "4")
    early_max_spread_pct = env_float("EARLY_MAX_SPREAD_PCT", env("MAX_SPREAD_PCT", "0.004"))
    early_min_strength = min_strength_rank(env("EARLY_MIN_STRENGTH", "OK"))

    # NEW: click-to-confirm settings
    enable_click_confirm = env_bool("ENABLE_CLICK_CONFIRM", "ON")
    confirm_window_sec = env_int("CONFIRM_WINDOW_SEC", "18")         # monitor after click
    confirm_trigger_bps = env_float("CONFIRM_TRIGGER_BPS", "10")     # move in your favor to confirm (10 bps = 0.10%)
    confirm_max_pullback_bps = env_float("CONFIRM_MAX_PULLBACK_BPS", "8")  # fail-fast against you
    confirm_poll_sec = env_int("CONFIRM_POLL_SEC", "2")
    confirm_max_spread_pct = env_float("CONFIRM_MAX_SPREAD_PCT", env("MAX_SPREAD_PCT", "0.004"))

    # Telegram
    tg = TelegramAPI(
        token=env("TELEGRAM_BOT_TOKEN"),
        chat_id=env("TELEGRAM_CHAT_ID"),
    )

    client = StockHistoricalDataClient(key_id, secret)

    # start telegram polling thread (for button clicks)
    if enable_click_confirm:
        t_poll = threading.Thread(
            target=telegram_poll_loop,
            daemon=True,
            args=(
                client,
                tg,
                confirm_window_sec,
                confirm_trigger_bps,
                confirm_max_pullback_bps,
                confirm_poll_sec,
                confirm_max_spread_pct,
            ),
        )
        t_poll.start()

    last_signal_time: dict[str, datetime] = {}
    last_signal_key: dict[str, str] = {}

    tg.send_message(
        "✅ البوت اشتغل | Bot Started\n"
        f"👀 Watching: {', '.join(tickers)}\n"
        f"⚙️ MODE: {mode}\n"
        f"⏱ Interval: {interval_sec}s | Lookback: {lookback_min}m\n"
        f"🎯 Threshold: {thresh_pct*100:.2f}%\n"
        f"🔥 Volume Mult: x{volume_mult} | Min Vol Ratio: x{min_vol_ratio}\n"
        f"🧠 Late-Entry Filter: abs(move {recent_window_min}m) <= {max_recent_move_pct*100:.2f}%\n"
        f"🕯️ Candle Filter: {candle_filter_mode} | ClosePosMin: {candle_close_pos_min}\n"
        f"⚡ Early MicroConfirm: {'ON' if early_mode_on else 'OFF'} "
        f"| Trigger: {early_trigger_bps}bps | Confirm: {micro_confirm_sec}s/{micro_confirm_bps}bps "
        f"| SpreadMax: {early_max_spread_pct} | MinStrength: {env('EARLY_MIN_STRENGTH','OK')}\n"
        f"🧷 Click Confirm: {'ON' if enable_click_confirm else 'OFF'} "
        f"| Window {confirm_window_sec}s | Trigger {confirm_trigger_bps}bps | Pullback {confirm_max_pullback_bps}bps\n"
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

                # ===== late-entry filter =====
                df_recent = df_all.tail(recent_window_min + 2)
                if len(df_recent) < 4:
                    continue

                price_then = float(df_recent["close"].iloc[0])
                recent_move = pct(price_now, price_then)

                if abs(recent_move) > max_recent_move_pct:
                    continue

                # ===== lookback MA =====
                df_lb = df_all.tail(lookback_min + 2).copy()
                if len(df_lb) < (lookback_min + 2):
                    continue

                ma = float(df_lb["close"].iloc[-(lookback_min + 1):-1].mean())
                d = pct(price_now, ma)

                # ===== volume baseline =====
                vol_last = float(df_lb["volume"].iloc[-2])
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

                    # micro-confirm only for EARLY
                    if early_mode_on and "EARLY" in mode_tag:
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

                    # NEW: add buttons on EARLY signals
                    reply_markup = None
                    if enable_click_confirm and "EARLY" in mode_tag:
                        reply_markup = build_entry_keyboard(sym, side)
                        msg += "\n\n🧷 اضغط زر الدخول إذا دخلت فعلاً عشان يجيك (تأكيد/تحذير) خلال ثواني."

                        with state_lock:
                            last_early_msg_ref[sym] = {
                                "side": side,
                                "price_hint": price_now,
                                "ts": now,
                                # message_id is not returned from sendMessage easily here without extra parsing,
                                # but it's okay; we handle edits via callback message_id.
                            }

                    tg.send_message(msg, reply_markup=reply_markup)

                    last_signal_time[sym] = now
                    last_signal_key[sym] = key

        except Exception as e:
            try:
                tg.send_message(f"⚠️ خطأ | Bot error: {type(e).__name__}: {e}")
            except Exception:
                pass

        time.sleep(interval_sec)


if __name__ == "__main__":
    main()
