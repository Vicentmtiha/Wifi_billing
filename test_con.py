import time
from librouteros import connect

try:
    api = connect(host='192.168.122.218', username='admin', password='Janet@123')
    unique_name = f"test_{int(time.time())}"
    
    print(f"Naongeza user: {unique_name}...")
    
    # Hapa tumeweka ndani ya try-except ili tuone kosa likitokea
    try:
        api(('/ip', 'hotspot', 'user', 'add'), name=unique_name, password='123', profile='default')
        print("User ameongezeka kwa mafanikio!")
    except Exception as e:
        print(f"!!! MIKROTIK IMEKATAA: {e}")

    print("\nOrodha ya watumiaji na sifa zao (Full Details):")
    users = api('/ip/hotspot/user/print')
    for user in users:
        # Hii itaprint kila kitu kuhusu kila user
        print(f"Name: {user.get('name')} | Profile: {user.get('profile')} | Server: {user.get('server')}")
except Exception as e:
    print(f"Tatizo la connection: {e}")
