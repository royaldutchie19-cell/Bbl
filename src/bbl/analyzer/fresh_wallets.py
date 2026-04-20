"""Detect suspicious fresh wallets — new addresses that are immediately profitable.

A fresh wallet with high PnL, many trades, and a short history suggests an
experienced trader rotating into a new account.
"""

from __future__ import annotations

import logging
import math
import time

from bbl.storage import Database

log = logging.getLogger(__name__)

# language=sql
_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS fresh_wallet_signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               INTEGER NOT NULL,
    address          TEXT NOT NULL,
    wallet_age_days  REAL,
    trade_count      INTEGER,
    total_pnl        REAL,
    total_volume     REAL,
    win_rate         REAL,
    pnl_per_day      REAL,
    signal_strength  REAL,
    note             TEXT
);
CREATE INDEX IF NOT EXISTS idx_fws_ts       ON fresh_wallet_signals(ts DESC);
CREATE INDEX IF NOT EXISTS idx_fws_address  ON fresh_wallet_signals(address);
CREATE INDEX IF NOT EXISTS idx_fws_strength ON fresh_wallet_signals(signal_strength DESC);
"""


def _compute_signal_strength(
    total_pnl: float,
    win_rate: float | None,
    wallet_age_days: float,
    trade_count: int,
) -> float:
    """Compute a 0..1 signal strength score for a fresh wallet.

    Higher PnL (log scale), higher win rate, younger wallet, and more trades
    all push the score higher.
    """
    # PnL component: log10 scale, 1000 -> 0.3, 10000 -> 0.6, 100000 -> 0.9
    pnl_score = min(math.log10(max(total_pnl, 1)) / 5.5, 1.0)

    # Win-rate component: 0.5 is neutral, 0.8+ is strong
    if win_rate is not None:
        wr_score = max(0.0, (win_rate - 0.4) / 0.6)
    else:
        wr_score = 0.3  # unknown — mild default

    # Youth component: younger = stronger signal (1 day = 1.0, 30 days = ~0.3)
    youth_score = max(0.0, 1.0 - (wallet_age_days / 45.0))

    # Trade confidence: more trades = more confident the signal is real
    trade_score = min(math.log2(max(trade_count, 1)) / 8.0, 1.0)

    raw = 0.35 * pnl_score + 0.25 * wr_score + 0.25 * youth_score + 0.15 * trade_score
    return max(0.0, min(1.0, raw))


def detect_fresh_wallets(
    db: Database,
    max_age_days: int = 30,
    min_pnl: float = 1000,
) -> int:
    """Detect fresh wallets with suspiciously high profitability.

    Returns the number of new fresh-wallet signals inserted.
    """
    now = int(time.time())
    cutoff_ts = now - max_age_days * 86400

    db.conn.executescript(_CREATE_TABLE)

    # Find young, profitable, active wallets.
    rows = db.conn.execute(
        """
        SELECT t.address,
               t.first_seen_ts,
               t.trade_count,
               t.total_pnl_usdc,
               t.total_volume_usdc,
               tm.win_rate
          FROM traders t
          LEFT JOIN trader_metrics tm ON tm.address = t.address
         WHERE t.first_seen_ts > ?
           AND t.total_pnl_usdc > ?
           AND t.trade_count >= 5
        """,
        (cutoff_ts, min_pnl),
    ).fetchall()

    # Addresses already recorded — avoid duplicates.
    existing = {
        r[0]
        for r in db.conn.execute(
            "SELECT DISTINCT address FROM fresh_wallet_signals"
        ).fetchall()
    }

    count = 0
    with db.tx() as cur:
        for row in rows:
            address = row["address"]
            if address in existing:
                continue

            first_seen_ts = int(row["first_seen_ts"])
            trade_count = int(row["trade_count"])
            total_pnl = float(row["total_pnl_usdc"])
            total_volume = float(row["total_volume_usdc"] or 0)
            win_rate = float(row["win_rate"]) if row["win_rate"] is not None else None

            wallet_age_days = max((now - first_seen_ts) / 86400.0, 0.01)
            pnl_per_day = total_pnl / wallet_age_days

            signal_strength = _compute_signal_strength(
                total_pnl, win_rate, wallet_age_days, trade_count,
            )

            note = (
                f"age={wallet_age_days:.1f}d, "
                f"pnl=${total_pnl:,.0f}, "
                f"pnl/d=${pnl_per_day:,.0f}, "
                f"trades={trade_count}"
            )

            cur.execute(
                """
                INSERT INTO fresh_wallet_signals
                    (ts, address, wallet_age_days, trade_count, total_pnl,
                     total_volume, win_rate, pnl_per_day, signal_strength, note)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    address,
                    round(wallet_age_days, 4),
                    trade_count,
                    round(total_pnl, 2),
                    round(total_volume, 2),
                    round(win_rate, 4) if win_rate is not None else None,
                    round(pnl_per_day, 2),
                    round(signal_strength, 4),
                    note,
                ),
            )
            count += 1

    log.info("detected %d fresh wallet signals", count)
    return count
