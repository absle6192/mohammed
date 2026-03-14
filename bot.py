import requests

url = "https://demo.tradovateapi.com/v1/auth/accesstokenrequest"

payload = {
    "name": "MFFUmFjuXfihEG",
    "password": "V+TT1?8wSnqrv",
    "appId": "SampleApp",
    "appVersion": "1.0",
    "deviceId": "test-device"
}

headers = {
    "Content-Type": "application/json"
}

r = requests.post(url, json=payload, headers=headers)

print("Status Code:", r.status_code)
print("Response:", r.text)
