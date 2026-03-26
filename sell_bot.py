import time
import requests
from alpaca_trade_api import REST

API_KEY = "YOUR_KEY"
API_SECRET = "YOUR_SECRET"
BASE_URL = "https://paper-api.alpaca.markets"

api = REST(API_KEY, API_SECRET, BASE_URL)

WEBHOOK_URL = "YOUR_WEBHOOK_URL"

state = {}

def send_alert(msg):
    try:
        requests.post(WEBHOOK_URL, json={"content": msg})
    except:
        pass

def sell(symbol, qty, profit, reason=""):
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side='sell',
            type='market',
            time_in_force='day'
        )

        msg = f"🔴 SOLD {symbol} | Qty: {qty} | Profit: {round(profit,2)}$ {reason}"
        print(msg)
        send_alert(msg)

    except Exception as e:
        print("Sell error:", e)

while True:
    try:
        positions = api.list_positions()

        for pos in positions:
            symbol = pos.symbol
            qty = int(float(pos.qty))
            profit = float(pos.unrealized_pl)

            if symbol not in state:
                state[symbol] = {
                    "highest": profit,
                    "partial_sold": False
                }

            s = state[symbol]

            if profit > s["highest"]:
                s["highest"] = profit

            # Stop Loss
            if profit <= -12:
                sell(symbol, qty, profit, "(Stop Loss)")
                state.pop(symbol, None)
                continue

            # Partial Sell
            if profit >= 40 and not s["partial_sold"]:
                half = qty // 2
                if half > 0:
                    sell(symbol, half, profit/2, "(Partial Sell)")
                    s["partial_sold"] = True

            # Trailing
            if profit >= 7:
                gap = 2
                if profit >= 15:
                    gap = 3
                if profit >= 25:
                    gap = 5

                if profit <= s["highest"] - gap:
                    sell(symbol, qty, profit, "(Trailing Sell)")
                    state.pop(symbol, None)
                    continue

        time.sleep(2)

    except Exception as e:
        print("Error:", e)
        time.sleep(5)
