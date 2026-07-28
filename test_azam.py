import os
import requests
from dotenv import load_dotenv

load_dotenv()

# Jina lililorekebishwa kufanana na portal ya AzamPay
AZAMPAY_APP_NAME = os.getenv("AZAMPAY_APP_NAME", "Wifi-billing").strip('"')
AZAMPAY_CLIENT_ID = os.getenv("AZAMPAY_CLIENT_ID", "").strip('"')
AZAMPAY_CLIENT_SECRET = os.getenv("AZAMPAY_CLIENT_SECRET", "").strip('"')

# URL sahihi ya Authentication kwenye Sandbox
AUTH_URL = "https://authenticator-sandbox.azampay.co.tz/Applink/GetToken"

def get_azampay_token():
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    # Format 1: Standard camelCase
    p1 = {
        "appName": AZAMPAY_APP_NAME,
        "clientId": AZAMPAY_CLIENT_ID,
        "clientSecret": AZAMPAY_CLIENT_SECRET
    }

    # Format 2: PascalCase
    p2 = {
        "AppName": AZAMPAY_APP_NAME,
        "ClientId": AZAMPAY_CLIENT_ID,
        "ClientSecret": AZAMPAY_CLIENT_SECRET
    }

    payloads = [("camelCase", p1), ("PascalCase", p2)]

    for name, payload in payloads:
        print(f"🔄 Inajaribu format: {name} kwa AppName: '{AZAMPAY_APP_NAME}'...")
        try:
            res = requests.post(AUTH_URL, json=payload, headers=headers, timeout=15)
            print(f"Status: {res.status_code} | Body: {res.text}")

            if res.status_code == 200:
                data = res.json()
                token = None
                if isinstance(data.get("data"), dict):
                    token = data["data"].get("accessToken")
                elif isinstance(data.get("data"), str):
                    token = data["data"]
                
                if token:
                    print(f"\n🎉 SUCCESS ({name})! Token imepatikana kwa mafanikio!")
                    return token
        except Exception as e:
            print(f"Error: {e}")

    return None

if __name__ == "__main__":
    get_azampay_token()
