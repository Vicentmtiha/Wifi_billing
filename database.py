import os
from pymongo import MongoClient

# Tumia local MongoDB moja kwa moja
MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")

# Unganisha na MongoDB
client = MongoClient(MONGO_URL)

# Chagua jina la database yako ya WiFi Billing
db = client["wifi_billing_db"]
