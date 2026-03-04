import os
import time
import pandas as pd
from binance.client import Client
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator

API_KEY = os.getenv("lBo7XbDqrUfGWREDnqWfrITKcrKUhxgIkXS1HSTT4RgYgqpPEIUvbHQxCjGoPI4x")
API_SECRET = os.getenv("hlaE0xJGxJLUF56ph9uE7T2ZvWPgIDsL1krO8LDxPLa9ZmI3bvHtCYQXWl90N7GE")

client = Client(API_KEY, API_SECRET)

SYMBOLS = ["BTCUSDT","DOGEUSDT","SOLUSDT"]

BUY_RSI = 35
TAKE_PROFIT = 1.12
STOP_LOSS = 0.90

positions = {}

def indicators(symbol):

    klines = client.get_klines(symbol=symbol, interval=Client.KLINE_INTERVAL_1HOUR, limit=200)

    df = pd.DataFrame(klines)

    close = df[4].astype(float)

    rsi = RSIIndicator(close).rsi().iloc[-1]

    ma20 = SMAIndicator(close, window=20).sma_indicator().iloc[-1]

    ma50 = SMAIndicator(close, window=50).sma_indicator().iloc[-1]

    price = close.iloc[-1]

    return price, rsi, ma20, ma50


def buy(symbol, usdt_amount):

    price = float(client.get_symbol_ticker(symbol=symbol)["price"])

    qty = usdt_amount / price

    order = client.order_market_buy(
        symbol=symbol,
        quantity=round(qty,6)
    )

    positions[symbol] = price

    print("BOUGHT",symbol,price)


def sell(symbol):

    balance = client.get_asset_balance(asset=symbol.replace("USDT",""))

    qty = float(balance["free"])

    order = client.order_market_sell(
        symbol=symbol,
        quantity=round(qty,6)
    )

    print("SOLD",symbol)

    positions.pop(symbol,None)


while True:

    for symbol in SYMBOLS:

        price,rsi,ma20,ma50 = indicators(symbol)

        print(symbol,price,"RSI",rsi)

        if symbol not in positions:

            if rsi < BUY_RSI and ma20 > ma50:

                buy(symbol,20)

        else:

            entry = positions[symbol]

            if price >= entry * TAKE_PROFIT:

                sell(symbol)

            elif price <= entry * STOP_LOSS:

                sell(symbol)

    time.sleep(3600)
