import os
import requests
from dotenv import load_dotenv

load_dotenv()

APP_NAME = "Wifi-billing"
CLIENT_ID = "3959b635-11b3-4a13-8f9e-663975b9ba1e"
SECRET_KEY = "ZQMabFHN1tJlsTZWX5tJrCgimM/l8MeXxXcxbK/AwnjKGJMastcx4prRzmRUmyrVEdIgZg2gcLEuGkntvgiYQSHz7NDqUqBJISPsWu5i9WH7fvZbEkVRLMHOLBmfE5L0/6JQhn0LZxXncXmtwDZhJOy0u1hSYZmnpEFop1K2hpzWuxSQY6JrLwhsFsF5SiDrhBo05TYLyW/Bf84piubfvgZrVzKDVYATZjDfpqhcCb8OAN3Myi9XXvzPKsvikySu+9jyg+TCrs3BocGxHOVe0uH9hDOCcGxyxEdSvxmVj/VKhjTC1Z+mU0nQUFxamgFaU4oXfv4OT4bI81py509c4qoQPdVy8xWCUYKpSwv4ESayNvLVlWPovfWa5+2XTpYQJzRP0A1zPnPkANs9gM+b3yKzIO85XTZt21uhvihXBCvbXgn68/SOcGT+D+XQ2Kt15uSABlAkp0DT0F08x9HOtWp1UNCKLvdWqgwaoPb5M6HjxtuE789cTMuS3CuABeX2n09HiAwdSPgmUyEZCu99FI415zh9tlfiaZVQ0S4G8OC/LvtckbupuNnsoTAngBSWsKeLyvWTewKROWHge1gPb7BJweONXgg4XijnUQ4shIa4+tchINamoFiz4xf/+js1F7SnQ2OwZr58bjqEE5XNLlDX6oD7/y51olBnNYg+PUs="

urls = [
    "https://authenticator-sandbox.azampay.co.tz/AppHeader/token",
    "https://authenticator.azampay.co.tz/AppHeader/token",
    "https://authenticator-sandbox.azampay.co.tz/azampay/mno/token"
]

payload = {
    "appName": APP_NAME,
    "clientId": CLIENT_ID,
    "clientSecret": SECRET_KEY
}

headers = {"Content-Type": "application/json"}

for url in urls:
    print(f"\nTesting: {url}")
    res = requests.post(url, json=payload, headers=headers)
    print(f"Status: {res.status_code} | Text: {res.text}")
