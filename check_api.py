import json
import urllib.request
from urllib.error import URLError

def get_trades(user):
    url = f"https://data-api.polymarket.com/trades?user={user}&limit=5"
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode('utf-8'))
            print(f"User {user} latest:")
            for t in data[:5]:
                ts = t.get("timestamp")
                side = t.get("side")
                size = t.get("size")
                print(f"   Unix ts: {ts} -> {side} {size}")
    except URLError as e:
        print(f"Error for {user}: {e.reason}")

print("Checking live trades directly from Polymarket API...")
get_trades("0x7b235aa8730fa67f815695746738fb14f7ce1efe")
get_trades("0xb76d3d5608c96389633b3b99efa0d93f373bfd8d")
get_trades("0xc1016d1bfc6244fd51fcf5c8dc1b10afc52be6d1")
