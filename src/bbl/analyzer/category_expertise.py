"""Per-category performance for each trader.

Answers: "How does trader X perform in crypto markets vs politics vs sports?"
Groups trades by (taker, market category) and computes volume, PnL, win/loss
based on market resolution, and other stats.
"""

from __future__ import annotations

import logging

from bbl.storage import Database

log = logging.getLogger(__name__)

# language=sql
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS trader_categories (
    address         TEXT NOT NULL,
    category        TEXT NOT NULL,
    trade_count     INTEGER,
    buy_volume      REAL,
    sell_volume     REAL,
    estimated_pnl   REAL,
    win_count       INTEGER,
    loss_count      INTEGER,
    win_rate        REAL,
    avg_entry_price REAL,
    markets_traded  INTEGER,
    first_trade_ts  INTEGER,
    last_trade_ts   INTEGER,
    PRIMARY KEY (address, category)
);
CREATE INDEX IF NOT EXISTS idx_tc_category_pnl ON trader_categories(category, estimated_pnl DESC);
CREATE INDEX IF NOT EXISTS idx_tc_address      ON trader_categories(address);
"""

# language=sql
_UPSERT_QUERY = """
INSERT INTO trader_categories (
    address, category,
    trade_count, buy_volume, sell_volume, estimated_pnl,
    win_count, loss_count, win_rate,
    avg_entry_price, markets_traded,
    first_trade_ts, last_trade_ts
)
SELECT
    t.taker                                           AS address,
    m.category,
    COUNT(*)                                          AS trade_count,
    COALESCE(SUM(CASE WHEN t.side = 'BUY'  THEN t.usdc_size ELSE 0 END), 0)  AS buy_volume,
    COALESCE(SUM(CASE WHEN t.side = 'SELL' THEN t.usdc_size ELSE 0 END), 0)  AS sell_volume,
    COALESCE(SUM(CASE WHEN t.side = 'SELL' THEN t.usdc_size ELSE 0 END), 0)
  - COALESCE(SUM(CASE WHEN t.side = 'BUY'  THEN t.usdc_size ELSE 0 END), 0)  AS estimated_pnl,
    COALESCE(SUM(CASE
        WHEN mt.winner IS NOT NULL AND (
            (t.side = 'BUY'  AND mt.winner = 1)
         OR (t.side = 'SELL' AND mt.winner = 0)
        ) THEN 1 ELSE 0
    END), 0)                                          AS win_count,
    COALESCE(SUM(CASE
        WHEN mt.winner IS NOT NULL AND (
            (t.side = 'BUY'  AND mt.winner = 0)
         OR (t.side = 'SELL' AND mt.winner = 1)
        ) THEN 1 ELSE 0
    END), 0)                                          AS loss_count,
    COALESCE(SUM(CASE
        WHEN mt.winner IS NOT NULL AND (
            (t.side = 'BUY'  AND mt.winner = 1)
         OR (t.side = 'SELL' AND mt.winner = 0)
        ) THEN 1 ELSE 0
    END), 0) * 1.0
  / NULLIF(
        COALESCE(SUM(CASE
            WHEN mt.winner IS NOT NULL AND (
                (t.side = 'BUY'  AND mt.winner = 1)
             OR (t.side = 'SELL' AND mt.winner = 0)
            ) THEN 1 ELSE 0
        END), 0)
      + COALESCE(SUM(CASE
            WHEN mt.winner IS NOT NULL AND (
                (t.side = 'BUY'  AND mt.winner = 0)
             OR (t.side = 'SELL' AND mt.winner = 1)
            ) THEN 1 ELSE 0
        END), 0),
    0)                                                AS win_rate,
    CASE WHEN SUM(CASE WHEN t.side = 'BUY' THEN t.size ELSE 0 END) > 0
         THEN SUM(CASE WHEN t.side = 'BUY' THEN t.usdc_size ELSE 0 END)
            / SUM(CASE WHEN t.side = 'BUY' THEN t.size ELSE 0 END)
         ELSE NULL END                                AS avg_entry_price,
    COUNT(DISTINCT t.condition_id)                    AS markets_traded,
    MIN(t.ts)                                         AS first_trade_ts,
    MAX(t.ts)                                         AS last_trade_ts
FROM trades t
LEFT JOIN markets m  ON m.condition_id = t.condition_id
LEFT JOIN market_tokens mt ON mt.token_id = t.token_id
WHERE t.taker IS NOT NULL
  AND t.condition_id IS NOT NULL
  AND m.category IS NOT NULL
GROUP BY t.taker, m.category
ON CONFLICT(address, category) DO UPDATE SET
    trade_count     = excluded.trade_count,
    buy_volume      = excluded.buy_volume,
    sell_volume     = excluded.sell_volume,
    estimated_pnl   = excluded.estimated_pnl,
    win_count       = excluded.win_count,
    loss_count      = excluded.loss_count,
    win_rate        = excluded.win_rate,
    avg_entry_price = excluded.avg_entry_price,
    markets_traded  = excluded.markets_traded,
    first_trade_ts  = excluded.first_trade_ts,
    last_trade_ts   = excluded.last_trade_ts
"""


def compute_category_expertise(db: Database) -> int:
    """Compute per-category performance for each trader. Returns rows written."""
    db.conn.executescript(_CREATE_TABLE)

    cur = db.conn.execute(_UPSERT_QUERY)
    total = cur.rowcount

    log.info("computed category expertise: %d rows", total)
    return total
