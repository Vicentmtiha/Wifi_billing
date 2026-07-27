import requests

url = "https://authenticator-sandbox.azampay.co.tz/AppRegistration/GenerateToken"
payload = {
    "appName": "CORE-WISP",
    "clientId": "1e22fa09-53d2-457e-b379-46637b91f728",
    "clientSecret": "ZA9tkbwAlNR1zdPk2RXbxWAUWN1wwzwickPDsvSJ3Uf4aIHsZkaAK19Akp0UJziSl4wXefs0+4yhRsKIcxMW/y1r4dojAgB17C2Z2CmS3QKZdXseEdZDwDG4M1vjgcmXbQsBQ/ZTc22+GKTr5FMZzY03NgdSicK/SQxJ2wDx8tv9f3alcVtnIyw16ww4b6KTDg3EfmvLVxPcSbKaId848HFyj6Z4zEja/8zTvMQDX9C6SOvUBroaOrUVrYF3C4yadDJkMMBU6uC+eizykOMVLpZ/z++bbaScn/AOeM3u8OgCqQ970F8WIuZ28UOmBSTIBiee0D5Kdl4RajPI9edIubtDpR2R3i3h4519xAF4ln7ZqN4RrojZvxNTnMLM9wcLqQw2j0Q0TDtVZb/7jDlWSBlDbzpScgCTDZT7hTHsqA6lk7mbCpl1cbc//8gvVW58RW2QzglFI0Yr7ZUfSVROiA68RcDqvpS0FCMUKX045k4hMW1FRSWi5iZ4ct4G3vlF5ol8yU4SZOL51Ue9UBThxs3aQZxX6nOBYRC1agFZpwCdVWuLKNbMOl/g1PpsLfU17FNXnkf+qphHX9/jcpJN8kcu2DU/BlONutW6qP8rAaLxT4eZqRd/iK4ZMM92mDkJZUUDJPOpoU4yCy+9XDLQ/LDaYvw0EFp6mP0mF7sZdxI="
}

try:
    response = requests.post(url, json=payload, timeout=10)
    print("Status Code:", response.status_code)
    print("Response:", response.json())
except Exception as e:
    print("Kosa:", e)
