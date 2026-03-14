import requests

def login():

    url = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"

    payload = {
        "name": "MFFUmFjuXfihEG",
        "password": "V+TT1?8wSnqrv",
        "appId": "Tradovate",
        "appVersion": "1.0",
        "deviceId": "test-device"
    }

    headers = {
        "Content-Type": "application/json"
    }

    r = requests.post(url, json=payload, headers=headers)

    data = r.json()

    print("Status Code:", r.status_code)
    print("Response:", data)

    if "accessToken" in data:
        token = data["accessToken"]
        print("LOGIN SUCCESS ✅")
        return token
    else:
        print("LOGIN FAILED ❌")
        return None


def main():

    token = login()

    if token:
        print("Token:", token)
        print("Bot connected successfully 🚀")
    else:
        print("Could not login to Tradovate ❌")


if __name__ == "__main__":
    main()
