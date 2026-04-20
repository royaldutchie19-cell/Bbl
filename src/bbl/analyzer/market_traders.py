"""Per-market trader rankings -- who dominates each market."""

from __future__ import annotations

import logging

from bbl.storage import Database

log = logging.getLogger(__name__)

# language=sql
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS market_traders (
    condition_id    TEXT NOT NULL,
    address         TEXT NOT NULL,
    category        TEXT,
    buy_volume      REAL,
    sell_volume     REAL,
    net_volume      REAL,
    trade_count     INTEGER,
    avg_buy_price   REAL,
    avg_sell_price  REAL,
    first_trade_ts  INTEGER,
    last_trade_ts   INTEGER,
    estimated_pnl   REAL,
    rank            INTEGER,
    PRIMARY KEY (condition_id, address)
);
CREATE INDEX IF NOT EXISTS idx_mt_condition ON market_traders(condition_id);
CREATE INDEX IF NOT EXISTS idx_mt_address   ON market_traders(address);
CREATE INDEX IF NOT EXISTS idx_mt_category  ON market_traders(category);
"""

# language=sql
_UPSERT_QUERY = """
INSERT INTO market_traders (
    condition_id, address, category,
    buy_volume, sell_volume, net_volume,
    trade_count, avg_buy_price, avg_sell_price,
    first_trade_ts, last_trade_ts,
    estimated_pnl, rank
)
SELECT
    t.condition_id,
    t.taker                                           AS address,
    m.category,
    COALESCE(SUM(CASE WHEN t.side = 'BUY'  THEN t.usdc_size ELSE 0 END), 0)  AS buy_volume,
    COALESCE(SUM(CASE WHEN t.side = 'SELL' THEN t.usdc_size ELSE 0 END), 0)  AS sell_volume,
    COALESCE(SUM(CASE WHEN t.side = 'BUY'  THEN t.usdc_size ELSE 0 END), 0)
  - COALESCE(SUM(CASE WHEN t.side = 'SELL' THEN t.usdc_size ELSE 0 END), 0)  AS net_volume,
    COUNT(*)                                          AS trade_count,
    CASE WHEN SUM(CASE WHEN t.side = 'BUY'  THEN t.usdc_size ELSE 0 END) > 0
         THEN SUM(CASE WHEN t.side = 'BUY'  THEN t.usdc_size ELSE 0 END)
            / SUM(CASE WHEN t.side = 'BUY'  THEN t.size ELSE 0 END)
         ELSE NULL END                                AS avg_buy_price,
    CASE WHEN SUM(CASE WHEN t.side = 'SELL' THEN t.usdc_size ELSE 0 END) > 0
         THEN SUM(CASE WHEN t.side = 'SELL' THEN t.usdc_size ELSE 0 END)
            / SUM(CASE WHEN t.side = 'SELL' THEN t.size ELSE 0 END)
         ELSE NULL END                                AS avg_sell_price,
    MIN(t.ts)                                         AS first_trade_ts,
    MAX(t.ts)                                         AS last_trade_ts,
    COALESCE(SUM(CASE WHEN t.side = 'SELL' THEN t.usdc_size ELSE 0 END), 0)
  - COALESCE(SUM(CASE WHEN t.side = 'BUY'  THEN t.usdc_size ELSE 0 END), 0)  AS estimated_pnl,
    ROW_NUMBER() OVER (
        PARTITION BY t.condition_id
        ORDER BY COUNT(*) DESC
    )                                                 AS rank
FROM trades t
LEFT JOIN markets m ON m.condition_id = t.condition_id
WHERE t.taker IS NOT NULL
  AND t.condition_id IS NOT NULL
GROUP BY t.condition_id, t.taker
ON CONFLICT(condition_id, address) DO UPDATE SET
    category       = excluded.category,
    buy_volume     = excluded.buy_volume,
    sell_volume    = excluded.sell_volume,
    net_volume     = excluded.net_volume,
    trade_count    = excluded.trade_count,
    avg_buy_price  = excluded.avg_buy_price,
    avg_sell_price = excluded.avg_sell_price,
    first_trade_ts = excluded.first_trade_ts,
    last_trade_ts  = excluded.last_trade_ts,
    estimated_pnl  = excluded.estimated_pnl,
    rank           = excluded.rank
"""


def compute_market_trader_rankings(db: Database) -> int:
    """Compute per-market trader rankings from trade data. Returns rows written."""
    db.conn.executescript(_CREATE_TABLE)

    cur = db.conn.execute(_UPSERT_QUERY)
    total = cur.rowcount

    log.info("computed market trader rankings: %d rows", total)
    return total
