import os
import requests
import time

# جلب التوكن من Environment Variables
TOKEN = os.getenv("TRADOVATE_TOKEN")

# رابط API للتجريبي
BASE_URL = "https://demo.tradovateapi.com/v1"

headers = {
    "Authorization": f"Bearer {TOKEN}",
    "Content-Type": "application/json"
}

def check_connection():
    url = f"{BASE_URL}/user/list"
    r = requests.get(url, headers=headers)

    if r.status_code == 200:
        print("✅ Connected to Tradovate")
        print(r.json())
    else:
        print("❌ Connection failed")
        print(r.text)

def main():
    print("Starting bot...")
    check_connection()

    while True:
        print("Bot running...")
        time.sleep(60)

if __name__ == "__main__":
    main()
