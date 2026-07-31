from pymongo import MongoClient
import os

# Weka ile Connection String ya MongoDB Atlas hapa au kwenye Render Environment Variables
# Badilisha <username>, <password>, na kiungo chako halisi cha cluster
MONGO_URL = os.getenv("MONGO_URL", "mongodb+srv://<username>:<password>@cluster0.xxxxx.mongodb.net/?retryWrites=true&w=majority")

# Unganisha na MongoDB Atlas
client = MongoClient(MONGO_URL)

# Chagua jina la database yako
db = client["wifi_billing_db"]

# Hakikisha muunganiko upo sawa
try:
    client.admin.command('ping')
    print("MongoDB imegunganishwa kikamilifu kwenye mfumo!")
except Exception as e:
    print(f"Imeshindwa kuunganisha na MongoDB: {e}")
