import os, time, math
import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import SimpleConnectionPool
from binance.client import Client
from binance.exceptions import BinanceAPIException
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator, MACD

# =========================
# ENV (strict)
# =========================
def env_required(name: str) -> str:
    v = os.getenv(name)
    if v is None or not v.strip():
        raise SystemExit(f"Missing required env var: {name}")
    return v.strip()

API_KEY = env_required("BINANCE_API_KEY")
API_SECRET = env_required("BINANCE_API_SECRET")
DATABASE_URL = env_required("DATABASE_URL")

TESTNET = (os.getenv("BINANCE_TESTNET", "false").strip().lower() == "true")

TRADE_MODE = (env_required("TRADE_MODE").lower() == "true")
USDT_PER_TRADE = float(env_required("USDT_PER_TRADE"))
MAX_OPEN = int(env_required("MAX_OPEN_POSITIONS"))

RSI_BUY = float(env_required("RSI_BUY"))  # e.g. 45
TRAIL_PCT = float(env_required("TRAIL_PCT"))  # e.g. 0.04 (4%)
TRAIL_ACTIVATE_PCT = float(env_required("TRAIL_ACTIVATE_PCT"))  # e.g. 0.02 (2%)

UNIVERSE_REFRESH_SEC = int(env_required("UNIVERSE_REFRESH_SEC"))  # 21600 = 6h
TOP_N = int(env_required("TOP_N"))
MIN_QUOTE_VOL = float(env_required("MIN_QUOTE_VOL"))

TP_PCT = float(os.getenv("TAKE_PROFIT_PCT", "0.12"))
SL_PCT = float(os.getenv("STOP_LOSS_PCT", "0.10"))
FEE_PCT = float(os.getenv("FEE_PCT", "0.001"))

SLEEP_SECONDS = int(os.getenv("SLEEP_SECONDS", "3600").strip())
API_DELAY_SEC = float(os.getenv("API_DELAY_SEC", "0.12").strip())  # delay بين طلبات Binance
SIDEWAYS_PCT = float(os.getenv("SIDEWAYS_PCT", "0.025").strip())   # 2.5% بدل 1%
MAX_HOLD_DAYS = int(os.getenv("MAX_HOLD_DAYS", "7").strip())       # إغلاق إجباري بعد 7 أيام

INTERVAL = Client.KLINE_INTERVAL_1HOUR
LIMIT = 250

# =========================
# Binance client
# =========================
client = Client(API_KEY, API_SECRET, testnet=TESTNET)

def api_sleep():
    # حماية بسيطة ضد rate limit
    if API_DELAY_SEC > 0:
        time.sleep(API_DELAY_SEC)

# =========================
# PostgreSQL connection pool
# =========================
# minconn=1 maxconn=5 كافي لبوت واحد
pool = SimpleConnectionPool(
    minconn=1,
    maxconn=5,
    dsn=DATABASE_URL,
    sslmode="require"
)

def with_conn(fn):
    """Decorator-like helper: acquires a pooled connection and returns it safely."""
    conn = pool.getconn()
    try:
        return fn(conn)
    finally:
        pool.putconn(conn)

def pg_init():
    def _init(conn):
        cur = conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_positions (
          symbol        TEXT PRIMARY KEY,
          qty           DOUBLE PRECISION NOT NULL,
          entry_price   DOUBLE PRECISION NOT NULL,
          highest_price DOUBLE PRECISION NOT NULL,
          opened_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
          updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_trades (
          id            BIGSERIAL PRIMARY KEY,
          symbol        TEXT NOT NULL,
          side          TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
          qty           DOUBLE PRECISION,
          price         DOUBLE PRECISION,
          reason        TEXT,
          created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
        );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_bot_trades_symbol_time ON bot_trades(symbol, created_at DESC);")
        conn.commit()
        cur.close()
    with_conn(_init)

def load_positions() -> dict:
    def _load(conn):
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT symbol, qty, entry_price, highest_price,
                   EXTRACT(EPOCH FROM opened_at)::bigint AS opened_at
            FROM bot_positions;
        """)
        rows = cur.fetchall()
        cur.close()
        return {
            r["symbol"]: {
                "qty": float(r["qty"]),
                "entry_price": float(r["entry_price"]),
                "highest_price": float(r["highest_price"]),
                "opened_at": int(r["opened_at"])
            }
            for r in rows
        }
    return with_conn(_load)

def upsert_position(symbol: str, qty: float, entry: float, high: float):
    def _up(conn):
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO bot_positions(symbol, qty, entry_price, highest_price)
        VALUES (%s,%s,%s,%s)
        ON CONFLICT(symbol) DO UPDATE SET
          qty=EXCLUDED.qty,
          entry_price=EXCLUDED.entry_price,
          highest_price=EXCLUDED.highest_price,
          updated_at=NOW();
        """, (symbol, qty, entry, high))
        conn.commit()
        cur.close()
    with_conn(_up)

def delete_position(symbol: str):
    def _del(conn):
        cur = conn.cursor()
        cur.execute("DELETE FROM bot_positions WHERE symbol=%s;", (symbol,))
        conn.commit()
        cur.close()
    with_conn(_del)

def log_trade(symbol: str, side: str, qty: float | None, price: float | None, reason: str):
    def _log(conn):
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO bot_trades(symbol, side, qty, price, reason)
        VALUES (%s,%s,%s,%s,%s);
        """, (symbol, side, qty, price, reason))
        conn.commit()
        cur.close()
    with_conn(_log)

# Init DB and positions
pg_init()
positions = load_positions()

# =========================
# Universe refresh every 6h
# =========================
universe = []
universe_last_refresh = 0

def is_bad_symbol(sym: str) -> bool:
    bad_suffix = ["UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT"]
    if any(sym.endswith(x) for x in bad_suffix):
        return True
    if sym in ("USDCUSDT", "TUSDUSDT", "FDUSDUSDT", "BUSDUSDT"):
        return True
    return False

def refresh_universe_if_needed():
    global universe, universe_last_refresh
    now = time.time()
    if universe and (now - universe_last_refresh) < UNIVERSE_REFRESH_SEC:
        return

    api_sleep()
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
    universe = [s for s, _ in rows[:TOP_N]]
    universe_last_refresh = now
    print(f"Universe refreshed ({len(universe)}) at {time.strftime('%Y-%m-%d %H:%M:%S')}")

# =========================
# Indicators + Signals
# =========================
def fetch_klines(symbol: str) -> pd.DataFrame:
    api_sleep()
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
        "ma20_prev": float(ma20.iloc[-2]),
        "ma50_prev": float(ma50.iloc[-2]),
    }

def trend_mode_signal(m):
    trend_ok = m["ma50"] > m["ma200"]
    dip_ok = m["rsi"] < RSI_BUY
    cross_ok = (m["ma20"] > m["ma50"]) or (m["ma20_prev"] <= m["ma50_prev"] and m["ma20"] > m["ma50"])
    macd_ok = m["hist"] > m["hist_prev"]
    return trend_ok and dip_ok and cross_ok and macd_ok

def range_mode_signal(m):
    # سوق جانبي إذا MA50 قريب من MA200 بنسبة SIDEWAYS_PCT (مثلاً 2.5%)
    sideways = abs(m["ma50"] - m["ma200"]) / max(m["ma200"], 1e-9) < SIDEWAYS_PCT
    dip_ok = m["rsi"] < RSI_BUY
    macd_ok = m["hist"] > m["hist_prev"]
    return sideways and dip_ok and macd_ok

def buy_signal(m):
    return trend_mode_signal(m) or range_mode_signal(m)

# =========================
# Trading helpers
# =========================
def get_free_balance(asset: str) -> float:
    api_sleep()
    b = client.get_asset_balance(asset=asset)
    return float(b.get("free", 0.0) or 0.0) if b else 0.0

def market_buy_quote(symbol: str, usdt_amount: float):
    api_sleep()
    order = client.order_market_buy(symbol=symbol, quoteOrderQty=usdt_amount)
    executed_qty = float(order.get("executedQty", 0.0) or 0.0)
    cumm_quote = float(order.get("cummulativeQuoteQty", 0.0) or 0.0)
    avg_price = (cumm_quote / executed_qty) if executed_qty > 0 else None
    return executed_qty, float(avg_price) if avg_price else None

def get_symbol_filters(symbol: str):
    api_sleep()
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
    floored = math.floor(qty / step) * step
    return round(floored, precision)

def cancel_open_orders(symbol: str):
    """Important: free balance could be zero if locked in open orders."""
    api_sleep()
    orders = client.get_open_orders(symbol=symbol)
    if not orders:
        return 0
    canceled = 0
    for o in orders:
        api_sleep()
        client.cancel_order(symbol=symbol, orderId=o["orderId"])
        canceled += 1
    if canceled:
        print(f"[{symbol}] Canceled open orders: {canceled}")
    return canceled

def market_sell_qty(symbol: str, qty: float) -> tuple[bool, float]:
    """Sell specific qty (prefer qty from saved position), after canceling open orders."""
    cancel_open_orders(symbol)

    step, min_qty = get_symbol_filters(symbol)
    qty2 = round_step(qty, step)
    if qty2 < min_qty:
        return False, qty2

    api_sleep()
    client.order_market_sell(symbol=symbol, quantity=qty2)
    return True, qty2

def should_take_profit(entry: float, last: float) -> bool:
    tp_gross = 1.0 + TP_PCT + (2 * FEE_PCT)
    return last >= entry * tp_gross

def should_stop_loss(entry: float, last: float) -> bool:
    sl_gross = 1.0 - SL_PCT - (2 * FEE_PCT)
    return last <= entry * sl_gross

def trailing_stop_hit(entry: float, highest: float, last: float) -> bool:
    if highest < entry * (1.0 + TRAIL_ACTIVATE_PCT):
        return False
    trail_level = highest * (1.0 - TRAIL_PCT)
    return last <= trail_level

def max_hold_hit(opened_at: int) -> bool:
    return (time.time() - opened_at) >= (MAX_HOLD_DAYS * 86400)

def pnl_pct(entry: float, sell_price: float) -> float:
    return (sell_price - entry) / entry * 100.0 if entry > 0 else 0.0

# =========================
# Main loop
# =========================
def run_once():
    refresh_universe_if_needed()
    print("\n==============================")
    print(f"SMART TRADE | mode={'REAL' if TRADE_MODE else 'DRY'} | tf=1h")
    print(f"Universe size={len(universe)} Open={list(positions.keys())}")
    print("==============================")

    # 1) Manage open positions
    for sym in list(positions.keys()):
        try:
            df = fetch_klines(sym)
            m = compute(df)
            entry = positions[sym]["entry_price"]
            highest = positions[sym]["highest_price"]
            last = m["last"]
            opened_at = positions[sym]["opened_at"]
            pos_qty = positions[sym]["qty"]

            # update highest
            if last > highest:
                highest = last
                positions[sym]["highest_price"] = highest
                upsert_position(sym, pos_qty, entry, highest)

            reason = None
            if should_take_profit(entry, last):
                reason = "TP"
            elif should_stop_loss(entry, last):
                reason = "SL"
            elif trailing_stop_hit(entry, highest, last):
                reason = "TRAIL"
            elif max_hold_hit(opened_at):
                reason = "MAX_HOLD"

            if reason:
                print(f"[{sym}] 🔻 EXIT {reason} entry={entry:.6f} high={highest:.6f} last={last:.6f}")
                if TRADE_MODE:
                    ok, sold_qty = market_sell_qty(sym, pos_qty)
                    if ok:
                        p = pnl_pct(entry, last)
                        log_trade(sym, "SELL", sold_qty, last, f"{reason} PnL={p:.2f}%")
                        print(f"[{sym}] SOLD ({reason}) qty={sold_qty} PnL={p:.2f}%")
                    else:
                        print(f"[{sym}] SELL skipped: qty too small after rounding ({sold_qty})")

                positions.pop(sym, None)
                delete_position(sym)
                continue

        except Exception as e:
            print(f"[{sym}] ❌ Manage ERROR: {e}")

    # 2) New entries
    if len(positions) >= MAX_OPEN:
        print(f"Max open positions reached ({MAX_OPEN}).")
        return

    candidates = []
    for sym in universe:
        if sym in positions:
            continue
        try:
            df = fetch_klines(sym)
            m = compute(df)
            if buy_signal(m):
                score = (m["rsi"], -(m["hist"] - m["hist_prev"]))
                candidates.append((score, sym, m))
        except Exception as e:
            print(f"[{sym}] ❌ Scan ERROR: {e}")

    if not candidates:
        print("No BUY candidates.")
        return

    candidates.sort(key=lambda x: x[0])
    slots = MAX_OPEN - len(positions)

    for _, sym, m in candidates[:slots]:
        print(f"[{sym}] ✅ BUY_CANDIDATE last={m['last']:.6f} RSI={m['rsi']:.2f} "
              f"MA20={m['ma20']:.6f} MA50={m['ma50']:.6f} MA200={m['ma200']:.6f} HIST={m['hist']:.6f}")

        if not TRADE_MODE:
            continue

        usdt_free = get_free_balance("USDT")
        if usdt_free < USDT_PER_TRADE:
            print(f"[{sym}] ⛔ Not enough USDT free={usdt_free:.2f}")
            continue

        try:
            qty, avg = market_buy_quote(sym, USDT_PER_TRADE)
            entry = avg if avg else m["last"]
            positions[sym] = {
                "qty": float(qty),
                "entry_price": float(entry),
                "highest_price": float(entry),
                "opened_at": int(time.time())
            }
            upsert_position(sym, float(qty), float(entry), float(entry))
            log_trade(sym, "BUY", float(qty), float(entry), "ENTRY")
            print(f"[{sym}] BOUGHT quote={USDT_PER_TRADE} qty={qty:.8f} entry={entry:.6f}")

        except BinanceAPIException as e:
            print(f"[{sym}] ❌ BUY API ERROR: {e}")
        except Exception as e:
            print(f"[{sym}] ❌ BUY ERROR: {e}")

if __name__ == "__main__":
    print("Starting container...")
    while True:
        run_once()
        time.sleep(SLEEP_SECONDS)
