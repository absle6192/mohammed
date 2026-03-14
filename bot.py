import os
import requests

USERNAME = os.getenv("TRADOVATE_USERNAME")
PASSWORD = os.getenv("TRADOVATE_PASSWORD")

def login():

    url = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"

    payload = {
        "name": USERNAME,
        "password": PASSWORD,
        "appId": "Sample App",
        "appVersion": "1.0",
        "deviceId": "123456789",
        "cid": 0
    }

    headers = {
        "Content-Type": "application/json"
    }

    r = requests.post(url, json=payload, headers=headers)

    print("Status Code:", r.status_code)
    print("Response:", r.text)

    data = r.json()

    if "accessToken" in data:
        token = data["accessToken"]
        print("Login success ✅")
        print("Token:", token)
        return token

    elif "p-ticket" in data:
        print("Login failed ❌")
        print("Server returned p-ticket instead of token")
        return None

    else:
        print("Unknown response:", data)
        return None


def main():

    print("Starting bot...")

    token = login()

    if token is None:
        print("Stopping bot بسبب فشل تسجيل الدخول")
        return

    print("Bot is running...")


if __name__ == "__main__":
    main()
