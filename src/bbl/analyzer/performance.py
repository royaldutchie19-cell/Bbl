"""Per-trader behavioral metrics.

For each wallet with >= min_trades fills we compute behavioral signals
that the Polymarket API does NOT provide:
    - trade count, USDC volume, unique markets
    - win rate (fraction of resolved-market positions on the winning side)
    - avg / median trade size
    - avg holding time (buy → sell pairing)
    - early-entry score (how early in a market's life they entered)
    - contrarian score (filled by patterns.py)

PnL and ROI come from the Polymarket data-api (traders.total_pnl_usdc),
not from our own FIFO reconstruction.
"""

from __future__ import annotations

import logging
import statistics as stats
import time
from collections import defaultdict
from dataclasses import dataclass

from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)


@dataclass
class _Fill:
    ts: int
    token_id: str | None
    side: str | None
    price: float
    size: float
    usdc: float


def compute_trader_metrics(cfg: Config, db: Database) -> int:
    """Recompute trader_metrics from scratch. Returns rows written."""
    min_trades = cfg.analyzer.min_trades_for_ranking
    min_volume = cfg.analyzer.min_volume_usdc

    trades_by_addr: dict[str, list[_Fill]] = defaultdict(list)
    for row in db.conn.execute(
        """
        SELECT taker, ts, token_id, side, price, size, usdc_size
        FROM trades WHERE taker IS NOT NULL
        """
    ):
        trades_by_addr[row["taker"]].append(
            _Fill(
                ts=row["ts"],
                token_id=row["token_id"],
                side=row["side"],
                price=float(row["price"] or 0),
                size=float(row["size"] or 0),
                usdc=float(row["usdc_size"] or 0),
            )
        )

    market_start_ts = _market_first_trade_ts(db)
    winner_tokens = _winner_tokens(db)
    loser_tokens = _loser_tokens(db)
    api_pnl = _api_pnl_by_addr(db)
    now = int(time.time())

    rows_written = 0
    with db.tx() as cur:
        for addr, fills in trades_by_addr.items():
            if len(fills) < min_trades:
                continue
            volume = sum(f.usdc for f in fills)
            if volume < min_volume:
                continue

            pnl = api_pnl.get(addr, 0.0)
            roi = pnl / volume if volume > 0 else 0.0

            win_rate = _win_rate_resolved(fills, winner_tokens)
            holding_secs = _holding_times(fills)
            avg_clv, clv_pos_rate = _clv(fills, winner_tokens, loser_tokens)

            sizes = [f.usdc for f in fills if f.usdc > 0]
            avg_size = sum(sizes) / len(sizes) if sizes else 0.0
            med_size = stats.median(sizes) if sizes else 0.0

            markets_traded = len({f.token_id for f in fills if f.token_id})
            avg_hold = (sum(holding_secs) / len(holding_secs)) if holding_secs else 0.0
            early_score = _early_entry_score(fills, market_start_ts)

            cur.execute(
                """
                INSERT OR REPLACE INTO trader_metrics (
                    address, computed_ts, window_start_ts, window_end_ts,
                    trade_count, volume_usdc, realized_pnl, roi, win_rate,
                    avg_trade_size, median_trade_size, max_drawdown, sharpe_like,
                    markets_traded, avg_holding_secs, early_entry_score,
                    contrarian_score, notes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    addr,
                    now,
                    min(f.ts for f in fills),
                    max(f.ts for f in fills),
                    len(fills),
                    volume,
                    pnl,
                    roi,
                    win_rate,
                    avg_size,
                    med_size,
                    avg_clv,         # repurpose max_drawdown column for avg_clv
                    clv_pos_rate,    # repurpose sharpe_like column for clv_positive_rate
                    markets_traded,
                    avg_hold,
                    early_score,
                    0.0,  # contrarian_score — filled by patterns.py
                    None,
                ),
            )
            rows_written += 1
    log.info("computed metrics for %d traders", rows_written)
    return rows_written


def _market_first_trade_ts(db: Database) -> dict[str, int]:
    return {
        r["token_id"]: r["first_ts"]
        for r in db.conn.execute(
            "SELECT token_id, MIN(ts) AS first_ts FROM trades "
            "WHERE token_id IS NOT NULL GROUP BY token_id"
        )
    }


def _winner_tokens(db: Database) -> set[str]:
    """Set of token_ids that resolved as winners."""
    return {
        r["token_id"]
        for r in db.conn.execute(
            "SELECT token_id FROM market_tokens WHERE winner = 1"
        )
    }


def _api_pnl_by_addr(db: Database) -> dict[str, float]:
    return {
        r["address"]: float(r["total_pnl_usdc"] or 0)
        for r in db.conn.execute(
            "SELECT address, total_pnl_usdc FROM traders WHERE total_pnl_usdc IS NOT NULL"
        )
    }


def _loser_tokens(db: Database) -> set[str]:
    """Set of token_ids that resolved as losers."""
    return {
        r["token_id"]
        for r in db.conn.execute(
            "SELECT token_id FROM market_tokens WHERE winner = 0"
        )
    }


def _win_rate_resolved(fills: list[_Fill], winner_tokens: set[str]) -> float:
    """Fraction of resolved-market BUY positions on the winning side."""
    wins = 0
    total = 0
    seen: set[str] = set()
    for f in fills:
        if f.side != "BUY" or not f.token_id:
            continue
        if f.token_id in seen:
            continue
        seen.add(f.token_id)
        if f.token_id in winner_tokens:
            wins += 1
            total += 1
        else:
            total += 1
    return wins / total if total > 0 else 0.0


def _clv(fills: list[_Fill], winner_tokens: set[str],
         loser_tokens: set[str]) -> tuple[float | None, float | None]:
    """Closing Line Value — how good were the entry prices vs. resolution.

    For BUY on a winner token: CLV = 1 - entry_price  (positive = good)
    For BUY on a loser token:  CLV = 0 - entry_price  (negative = bad)
    Unresolved tokens are skipped.

    Returns (avg_clv, clv_positive_rate).
    """
    resolved = winner_tokens | loser_tokens
    clvs: list[float] = []
    for f in fills:
        if f.side != "BUY" or not f.token_id or f.token_id not in resolved:
            continue
        if f.price <= 0:
            continue
        settle = 1.0 if f.token_id in winner_tokens else 0.0
        clvs.append(settle - f.price)
    if not clvs:
        return None, None
    avg = sum(clvs) / len(clvs)
    pos_rate = sum(1 for c in clvs if c > 0) / len(clvs)
    return round(avg, 6), round(pos_rate, 4)


def _holding_times(fills: list[_Fill]) -> list[float]:
    """Estimate holding times from BUY→SELL pairs per token (FIFO matching)."""
    by_token: dict[str, list[_Fill]] = defaultdict(list)
    for f in fills:
        if f.token_id:
            by_token[f.token_id].append(f)

    secs: list[float] = []
    for tfills in by_token.values():
        tfills.sort(key=lambda x: x.ts)
        buy_queue: list[tuple[float, int]] = []  # (size, ts)
        for f in tfills:
            if f.side == "BUY":
                buy_queue.append((f.size, f.ts))
            elif f.side == "SELL":
                remaining = f.size
                while remaining > 0 and buy_queue:
                    lot_size, lot_ts = buy_queue[0]
                    take = min(lot_size, remaining)
                    secs.append(max(0, f.ts - lot_ts))
                    remaining -= take
                    if take >= lot_size:
                        buy_queue.pop(0)
                    else:
                        buy_queue[0] = (lot_size - take, lot_ts)
    return secs


def _early_entry_score(fills: list[_Fill], market_start_ts: dict[str, int]) -> float:
    """0 = entered first second; 1 = entered much later. Lower is better."""
    ratios: list[float] = []
    by_token: dict[str, list[_Fill]] = defaultdict(list)
    for f in fills:
        if f.token_id:
            by_token[f.token_id].append(f)
    for tid, mfills in by_token.items():
        start = market_start_ts.get(tid)
        if start is None:
            continue
        my_first = min(f.ts for f in mfills)
        span = max(int(time.time()) - start, 86400)
        ratios.append(max(0.0, min(1.0, (my_first - start) / span)))
    if not ratios:
        return 1.0
    return sum(ratios) / len(ratios)
