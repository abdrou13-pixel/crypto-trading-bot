import os
from binance.client import Client

API_KEY = os.getenv("BINANCE_API_KEY", "SQsCSkgq9cWG4LegcWWFyy4EJJ6OCupvAdPy0Rf8m9PBKiVjK8TuAeVw9NCFjUHm")
API_SECRET = os.getenv("BINANCE_API_SECRET", "aUzQZ6QXyoabLLWoRwK0pormrcf5xJljYZOp9j8mDrFZOcoH5d6SSWHMpVtEJv4O")
TESTNET = os.getenv("BINANCE_TESTNET", "true").lower() == "true"

if not API_KEY or not API_SECRET:
    raise SystemExit("Missing BINANCE_API_KEY / BINANCE_API_SECRET in env vars")

client = Client(API_KEY, API_SECRET)

# Spot Testnet endpoint
if TESTNET:
    client.API_URL = "https://testnet.binance.vision/api"

# 1) Ping
print("Ping:", client.ping())

# 2) Server time
print("Server time:", client.get_server_time())

# 3) Account balances (show only non-zero)
acct = client.get_account()
balances = [(b["asset"], b["free"], b["locked"]) for b in acct["balances"]
            if float(b["free"]) > 0 or float(b["locked"]) > 0]
print("Non-zero balances:", balances)

# 4) Get price sample
symbol = "BTCUSDT"
price = client.get_symbol_ticker(symbol=symbol)
print(f"{symbol} price:", price["price"])
