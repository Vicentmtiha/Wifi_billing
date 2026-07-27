import librouteros

try:
    connection = librouteros.connect(
        host='192.168.122.218',
        username='admin',
        password='Janet@123'
    )
    print("Imefaulu! Mfumo unaongea na MikroTik safi kabisa.")
except Exception as e:
    print(f"Imegoma: {e}")
