"""Pair cost arbitrage detection.

On Polymarket, YES + NO should always sum to 1.00.  When the combined
cost is less than 1.00 there is risk-free profit available by buying
both sides.  This module detects those opportunities using both the
markets table (mid prices) and price_snapshots (best ask).
"""

from __future__ import annotations

import logging
import time

from bbl.storage import Database

log = logging.getLogger(__name__)


def detect_arbitrage_opportunities(db: Database, min_gap: float = 0.02) -> int:
    """Scan active markets for pair-cost arbitrage. Returns opportunities found."""
    now = int(time.time())

    db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS arbitrage_signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            condition_id    TEXT NOT NULL,
            ts              INTEGER NOT NULL,
            yes_price       REAL,
            no_price        REAL,
            pair_cost       REAL,
            gap             REAL,
            best_yes_ask    REAL,
            best_no_ask     REAL,
            ask_pair_cost   REAL,
            estimated_profit_pct REAL
        );
        CREATE INDEX IF NOT EXISTS idx_arb_ts   ON arbitrage_signals(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_arb_cond ON arbitrage_signals(condition_id, ts);
    """)

    rows = db.conn.execute(
        """
        SELECT condition_id, yes_price, no_price
          FROM markets
         WHERE active = 1 AND closed = 0
           AND yes_price IS NOT NULL AND no_price IS NOT NULL
        """
    ).fetchall()

    count = 0
    with db.tx() as cur:
        for r in rows:
            cid = r["condition_id"]
            yes_p = float(r["yes_price"])
            no_p = float(r["no_price"])
            pair_cost = yes_p + no_p
            gap = 1.0 - pair_cost

            best_yes_ask, best_no_ask, ask_pair_cost = _get_best_asks(db, cid)

            if gap < min_gap and (ask_pair_cost is None or 1.0 - ask_pair_cost < min_gap):
                continue

            profit_pct = None
            if ask_pair_cost is not None and ask_pair_cost < 1.0:
                profit_pct = round((1.0 - ask_pair_cost) / ask_pair_cost * 100, 4)
            elif gap >= min_gap:
                profit_pct = round(gap / pair_cost * 100, 4)

            cur.execute(
                """
                INSERT INTO arbitrage_signals
                    (condition_id, ts, yes_price, no_price, pair_cost, gap,
                     best_yes_ask, best_no_ask, ask_pair_cost, estimated_profit_pct)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    cid, now, yes_p, no_p,
                    round(pair_cost, 6), round(gap, 6),
                    best_yes_ask, best_no_ask, ask_pair_cost,
                    profit_pct,
                ),
            )
            count += 1

    log.info("detected %d arbitrage opportunities", count)
    return count


def _get_best_asks(db: Database, condition_id: str) -> tuple[float | None, float | None, float | None]:
    """Get latest best_ask for each side of a market from price_snapshots."""
    tokens = db.conn.execute(
        "SELECT token_id, outcome FROM market_tokens WHERE condition_id = ?",
        (condition_id,),
    ).fetchall()

    yes_ask = None
    no_ask = None

    for tok in tokens:
        outcome = (tok["outcome"] or "").lower()
        snap = db.conn.execute(
            """
            SELECT best_ask FROM price_snapshots
             WHERE token_id = ? AND best_ask IS NOT NULL
             ORDER BY ts DESC LIMIT 1
            """,
            (tok["token_id"],),
        ).fetchone()
        if not snap:
            continue
        if outcome == "yes":
            yes_ask = float(snap["best_ask"])
        elif outcome == "no":
            no_ask = float(snap["best_ask"])

    ask_pair = round(yes_ask + no_ask, 6) if yes_ask is not None and no_ask is not None else None
    return yes_ask, no_ask, ask_pair
