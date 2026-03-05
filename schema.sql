CREATE TABLE IF NOT EXISTS bot_positions (
  symbol        TEXT PRIMARY KEY,
  qty           DOUBLE PRECISION NOT NULL,
  entry_price   DOUBLE PRECISION NOT NULL,
  highest_price DOUBLE PRECISION NOT NULL,
  opened_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS bot_trades (
  id            BIGSERIAL PRIMARY KEY,
  symbol        TEXT NOT NULL,
  side          TEXT NOT NULL CHECK (side IN ('BUY','SELL')),
  qty           DOUBLE PRECISION,
  price         DOUBLE PRECISION,
  reason        TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bot_trades_symbol_time ON bot_trades(symbol, created_at DESC);
