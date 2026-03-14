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
        "cid": 0
    }

    headers = {
        "Content-Type": "application/json"
    }

    r = requests.post(url, json=payload, headers=headers)

    print("Status Code:", r.status_code)
    print("Response:", r.text)

    data = r.json()

    if "accessToken" not in data:
        print("Login failed ❌")
        return None

    token = data["accessToken"]
    print("Login success ✅")
    print("Token:", token)

    return token


def main():
    print("Starting bot...")

    token = login()

    if token is None:
        print("Stopping bot بسبب فشل تسجيل الدخول")
        return

    print("Bot is running...")


if __name__ == "__main__":
    main()
