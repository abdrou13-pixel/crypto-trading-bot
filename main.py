import os, time, math
import pandas as pd
from binance.client import Client
from binance.exceptions import BinanceAPIException
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator, MACD

API_KEY = (os.getenv("BINANCE_API_KEY") or "lBo7XbDqrUfGWREDnqWfrITKcrKUhxgIkXS1HSTT4RgYgqpPEIUvbHQxCjGoPI4x").strip()
API_SECRET = (os.getenv("BINANCE_API_SECRET") or "hlaE0xJGxJLUF56ph9uE7T2ZvWPgIDsL1krO8LDxPLa9ZmI3bvHtCYQXWl90N7GE").strip()
TESTNET = (os.getenv("BINANCE_TESTNET", "false").strip().lower() == "true")

TRADE_MODE = (os.getenv("TRADE_MODE", "false").strip().lower() == "true")
USDT_PER_TRADE = float(os.getenv("USDT_PER_TRADE", "20").strip())
MAX_OPEN = int(os.getenv("MAX_OPEN_POSITIONS", "5").strip())

TP_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.12").strip())
SL_PCT = float(os.getenv("STOP_LOSS_PCT", "0.10").strip())
FEE_PCT = float(os.getenv("FEE_PCT", "0.001").strip())  # 0.1% تقريباً

INTERVAL = Client.KLINE_INTERVAL_1HOUR
LIMIT = 250
SLEEP_SECONDS = 3600

TOP_N = 20
MIN_QUOTE_VOL = 50_000_000  # 50M USDT

client = Client(API_KEY, API_SECRET, testnet=TESTNET)

# تخزين الصفقات المفتوحة بالذاكرة (لاحقاً نربط PostgreSQL للتثبيت ضد restart)
positions = {}  # symbol -> dict(entry_price, qty, entry_time)

def is_bad_symbol(sym: str) -> bool:
    bad_suffix = ["UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"]
    if any(sym.endswith(x) for x in bad_suffix):
        return True
    if sym in ("USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "BUSDUSDT"):
        return True
    return False

def get_top_usdt_symbols(n=20):
    tickers = client.get_ticker()
    rows = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if is_bad_symbol(sym):
            continue
        qv = float(t.get("quoteVolume", 0.0) or 0.0)
        if qv < MIN_QUOTE_VOL:
            continue
        rows.append((sym, qv))
    rows.sort(key=lambda x: x[1], reverse=True)
    return [s for s, _ in rows[:n]]

def fetch_klines(symbol: str) -> pd.DataFrame:
    klines = client.get_klines(symbol=symbol, interval=INTERVAL, limit=LIMIT)
    df = pd.DataFrame(klines, columns=[
        "open_time","open","high","low","close","volume",
        "close_time","qav","trades","tbbav","tbqav","ignore"
    ])
    for c in ["close","high","low","volume"]:
        df[c] = df[c].astype(float)
    return df

def compute(df: pd.DataFrame):
    close = df["close"]

    rsi = RSIIndicator(close, window=14).rsi()
    ma20 = SMAIndicator(close, window=20).sma_indicator()
    ma50 = SMAIndicator(close, window=50).sma_indicator()
    ma200 = SMAIndicator(close, window=200).sma_indicator()

    macd_obj = MACD(close=close, window_slow=26, window_fast=12, window_sign=9)
    hist = macd_obj.macd_diff()

    return {
        "last": float(close.iloc[-1]),
        "rsi": float(rsi.iloc[-1]),
        "ma20": float(ma20.iloc[-1]),
        "ma50": float(ma50.iloc[-1]),
        "ma200": float(ma200.iloc[-1]),
        "hist": float(hist.iloc[-1]),
        "hist_prev": float(hist.iloc[-2]),
        "hist_prev2": float(hist.iloc[-3]),
        "ma20_prev": float(ma20.iloc[-2]),
        "ma50_prev": float(ma50.iloc[-2]),
    }

def buy_signal(m):
    # Trend قوي
    trend_ok = m["ma50"] > m["ma200"]
    # Dip
    dip_ok = m["rsi"] < 35
    # Cross/تحسن
    cross_ok = (m["ma20"] > m["ma50"]) or (m["ma20_prev"] <= m["ma50_prev"] and m["ma20"] > m["ma50"])
    # MACD يتحسن
    macd_ok = m["hist"] > m["hist_prev"]
    ok = trend_ok and dip_ok and cross_ok and macd_ok
    return ok, {"trend_ok":trend_ok,"dip_ok":dip_ok,"cross_ok":cross_ok,"macd_ok":macd_ok}

def get_symbol_filters(symbol: str):
    info = client.get_symbol_info(symbol)
    if not info:
        raise RuntimeError(f"Symbol info not found: {symbol}")
    lot = next((f for f in info["filters"] if f["filterType"] == "LOT_SIZE"), None)
    if not lot:
        raise RuntimeError(f"LOT_SIZE not found for {symbol}")
    step = float(lot["stepSize"])
    min_qty = float(lot["minQty"])
    return step, min_qty

def round_step(qty: float, step: float) -> float:
    if step <= 0:
        return qty
    precision = int(round(-math.log(step, 10), 0)) if step < 1 else 0
    # floor to step
    floored = math.floor(qty / step) * step
    return round(floored, precision)

def market_buy_quote(symbol: str, usdt_amount: float):
    # Market buy بــ quoteOrderQty لتفادي حساب الكمية يدويًا
    order = client.order_market_buy(symbol=symbol, quoteOrderQty=usdt_amount)
    # استخراج متوسط سعر تقريبي + كمية منفذة
    executed_qty = float(order.get("executedQty", 0.0) or 0.0)
    cumm_quote = float(order.get("cummulativeQuoteQty", 0.0) or 0.0)
    avg_price = (cumm_quote / executed_qty) if executed_qty > 0 else None
    return order, executed_qty, avg_price

def market_sell_base(symbol: str, qty: float):
    step, min_qty = get_symbol_filters(symbol)
    qty2 = round_step(qty, step)
    if qty2 < min_qty:
        raise RuntimeError(f"Sell qty too small after rounding: {qty2} < minQty {min_qty}")
    return client.order_market_sell(symbol=symbol, quantity=qty2)

def get_free_balance(asset: str) -> float:
    b = client.get_asset_balance(asset=asset)
    if not b:
        return 0.0
    return float(b.get("free", 0.0) or 0.0)

def should_take_profit(entry: float, last: float) -> bool:
    # نرفع هدف الربح قليلاً لتغطية رسوم شراء+بيع
    tp_gross = 1.0 + TP_PCT + (2 * FEE_PCT)
    return last >= entry * tp_gross

def should_stop_loss(entry: float, last: float) -> bool:
    # ننزل حد الخسارة قليلاً لتغطية الرسوم (تقريب)
    sl_gross = 1.0 - SL_PCT - (2 * FEE_PCT)
    return last <= entry * sl_gross

def early_exit_macd(m) -> bool:
    # خروج مبكر: MACD histogram ينخفض شمعتين + السعر تحت MA20
    return (m["hist"] < m["hist_prev"] < m["hist_prev2"]) and (m["last"] < m["ma20"])

def run_once():
    top = get_top_usdt_symbols(TOP_N)
    print("\n==============================")
    print(f"SMART TRADE | mode={'REAL' if TRADE_MODE else 'DRY'} | top={TOP_N} tf=1h")
    print(f"Open positions: {list(positions.keys())}")
    print("==============================")

    # 1) إدارة الصفقات المفتوحة (بيع)
    for sym in list(positions.keys()):
        try:
            df = fetch_klines(sym)
            m = compute(df)
            entry = positions[sym]["entry_price"]
            last = m["last"]

            if should_take_profit(entry, last):
                print(f"[{sym}] ✅ TAKE_PROFIT hit. entry={entry:.6f} last={last:.6f}")
                if TRADE_MODE:
                    base = sym.replace("USDT", "")
                    qty = get_free_balance(base)
                    if qty > 0:
                        market_sell_base(sym, qty)
                        print(f"[{sym}] SOLD (TP)")
                positions.pop(sym, None)
                continue

            if should_stop_loss(entry, last):
                print(f"[{sym}] 🛑 STOP_LOSS hit. entry={entry:.6f} last={last:.6f}")
                if TRADE_MODE:
                    base = sym.replace("USDT", "")
                    qty = get_free_balance(base)
                    if qty > 0:
                        market_sell_base(sym, qty)
                        print(f"[{sym}] SOLD (SL)")
                positions.pop(sym, None)
                continue

            if early_exit_macd(m):
                print(f"[{sym}] ⚠️ Early exit (MACD weakening). entry={entry:.6f} last={last:.6f}")
                if TRADE_MODE:
                    base = sym.replace("USDT", "")
                    qty = get_free_balance(base)
                    if qty > 0:
                        market_sell_base(sym, qty)
                        print(f"[{sym}] SOLD (MACD)")
                positions.pop(sym, None)
                continue

        except Exception as e:
            print(f"[{sym}] ❌ Manage ERROR: {e}")

    # 2) الدخول (شراء) — فقط إذا ما تجاوزنا MAX_OPEN
    if len(positions) >= MAX_OPEN:
        print(f"Max open positions reached ({MAX_OPEN}). No new entries.")
        return

    candidates = []
    for sym in top:
        if sym in positions:
            continue
        try:
            df = fetch_klines(sym)
            m = compute(df)
            ok, dbg = buy_signal(m)
            if ok:
                # ترتيب الأفضل: RSI أقل + تحسن MACD أكبر
                score = (m["rsi"], -(m["hist"] - m["hist_prev"]))
                candidates.append((score, sym, m, dbg))
        except Exception as e:
            print(f"[{sym}] ❌ Scan ERROR: {e}")

    if not candidates:
        print("No BUY candidates.")
        return

    candidates.sort(key=lambda x: x[0])
    slots = MAX_OPEN - len(positions)
    chosen = candidates[:slots]

    for _, sym, m, dbg in chosen:
        print(f"[{sym}] ✅ BUY_CANDIDATE last={m['last']:.6f} RSI={m['rsi']:.2f} "
              f"MA20={m['ma20']:.6f} MA50={m['ma50']:.6f} MA200={m['ma200']:.6f} "
              f"HIST={m['hist']:.6f}->{m['hist_prev']:.6f} dbg={dbg}")

        if not TRADE_MODE:
            continue

        try:
            usdt_free = get_free_balance("USDT")
            if usdt_free < USDT_PER_TRADE:
                print(f"[{sym}] ⛔ Not enough USDT. free={usdt_free:.2f} need={USDT_PER_TRADE:.2f}")
                continue

            order, qty, avg = market_buy_quote(sym, USDT_PER_TRADE)
            entry_price = avg if avg else m["last"]
            positions[sym] = {"entry_price": float(entry_price), "qty": float(qty), "entry_time": time.time()}
            print(f"[{sym}] BOUGHT quote={USDT_PER_TRADE} qty={qty:.8f} entry={entry_price:.6f}")

        except BinanceAPIException as e:
            print(f"[{sym}] ❌ BUY API ERROR: {e}")
        except Exception as e:
            print(f"[{sym}] ❌ BUY ERROR: {e}")

if __name__ == "__main__":
    print("Starting container...")
    while True:
        run_once()
        time.sleep(SLEEP_SECONDS)
