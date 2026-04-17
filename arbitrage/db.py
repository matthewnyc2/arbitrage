from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from .config import settings

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS events (
  id TEXT PRIMARY KEY,
  slug TEXT,
  title TEXT,
  is_neg_risk INTEGER NOT NULL,
  end_date TEXT,
  active INTEGER NOT NULL DEFAULT 1,
  raw_json TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
  token_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  name TEXT NOT NULL,
  outcome_index INTEGER NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (event_id) REFERENCES events(id)
);
CREATE INDEX IF NOT EXISTS idx_outcomes_event ON outcomes(event_id);

CREATE TABLE IF NOT EXISTS opportunities (
  id TEXT PRIMARY KEY,
  detected_at TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_title TEXT NOT NULL,
  sum_vwap_asks TEXT NOT NULL,
  net_edge_bps INTEGER NOT NULL,
  max_baskets TEXT NOT NULL,
  expected_profit_usd TEXT NOT NULL,
  legs_json TEXT NOT NULL,
  acted_on INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_opps_detected ON opportunities(detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_opps_event ON opportunities(event_id);

CREATE TABLE IF NOT EXISTS baskets (
  id TEXT PRIMARY KEY,
  opportunity_id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  is_paper INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  basket_count TEXT NOT NULL,
  total_cost_usd TEXT NOT NULL,
  status TEXT NOT NULL,
  redeemed_at TEXT,
  redeemed_payout_usd TEXT,
  realized_pnl_usd TEXT,
  FOREIGN KEY (opportunity_id) REFERENCES opportunities(id)
);
CREATE INDEX IF NOT EXISTS idx_baskets_status ON baskets(status);
CREATE INDEX IF NOT EXISTS idx_baskets_event ON baskets(event_id);

CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  basket_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL,
  price TEXT NOT NULL,
  size TEXT NOT NULL,
  fee_usd TEXT NOT NULL,
  filled_at TEXT NOT NULL,
  FOREIGN KEY (basket_id) REFERENCES baskets(id)
);
CREATE INDEX IF NOT EXISTS idx_fills_basket ON fills(basket_id);

CREATE TABLE IF NOT EXISTS live_orders (
  id TEXT PRIMARY KEY,
  basket_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL,
  price TEXT NOT NULL,
  size TEXT NOT NULL,
  order_type TEXT NOT NULL,
  status TEXT NOT NULL,
  clob_order_id TEXT,
  tx_hash TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_live_orders_basket ON live_orders(basket_id);

CREATE TABLE IF NOT EXISTS resolutions (
  event_id TEXT PRIMARY KEY,
  winning_outcome_token_id TEXT,
  resolved_at TEXT NOT NULL,
  source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS denylist (
  event_id TEXT PRIMARY KEY,
  reason TEXT NOT NULL,
  added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daily_pnl (
  date TEXT PRIMARY KEY,
  paper_pnl_usd TEXT NOT NULL DEFAULT '0',
  live_pnl_usd TEXT NOT NULL DEFAULT '0',
  baskets_opened INTEGER NOT NULL DEFAULT 0,
  baskets_redeemed INTEGER NOT NULL DEFAULT 0
);
"""


async def init_db(db_path: Path | None = None) -> None:
    path = db_path or settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.executescript(SCHEMA_SQL)
        await conn.commit()


@asynccontextmanager
async def db_conn(db_path: Path | None = None) -> AsyncIterator[aiosqlite.Connection]:
    path = db_path or settings.db_path
    async with aiosqlite.connect(path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = aiosqlite.Row
        yield conn
