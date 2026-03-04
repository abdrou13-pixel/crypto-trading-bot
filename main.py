import os
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from binance.client import Client

API_KEY = (os.getenv("BINANCE_API_KEY") or ""SQsCSkgq9cWG4LegcWWFyy4EJJ6OCupvAdPy0Rf8m9PBKiVjK8TuAeVw9NCFjUHm"").strip()
API_SECRET = (os.getenv("BINANCE_API_SECRET") or ""aUzQZ6QXyoabLLWoRwK0pormrcf5xJljYZOp9j8mDrFZOcoH5d6SSWHMpVtEJv4O"").strip()
TESTNET = (os.getenv("BINANCE_TESTNET", "true").strip().lower() == "true")

client = Client(API_KEY, API_SECRET, testnet=TESTNET)

SYMBOLS = ["BTCUSDT", "DOGEUSDT", "SOLUSDT"]
INTERVAL = Client.KLINE_INTERVAL_1HOUR
LIMIT = 200  # كافي لـ MA50 و RSI

def fetch_klines(symbol: str) -> pd.DataFrame:
    klines = client.get_klines(symbol=symbol, interval=INTERVAL, limit=LIMIT)
    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])
    df["close"] = df["close"].astype(float)
    return df

def indicators(df: pd.DataFrame):
    close = df["close"]
    rsi = RSIIndicator(close, window=14).rsi().iloc[-1]
    ma20 = SMAIndicator(close, window=20).sma_indicator().iloc[-1]
    ma50 = SMAIndicator(close, window=50).sma_indicator().iloc[-1]
    last = close.iloc[-1]
    return last, rsi, ma20, ma50

print("=== 1H Indicators ===")
for sym in SYMBOLS:
    df = fetch_klines(sym)
    last, rsi, ma20, ma50 = indicators(df)
    trend = "UP" if ma20 > ma50 else "DOWN"
    print(f"{sym} | last={last:.6f} | RSI14={rsi:.2f} | MA20={ma20:.6f} | MA50={ma50:.6f} | trend={trend}")
