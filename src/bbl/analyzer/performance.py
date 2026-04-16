"""Per-trader performance metrics.

Runs entirely in SQL + small python reductions — no pandas required for the
default path so the analyzer works in a minimal install.

For each wallet with >= min_trades_for_ranking fills we compute:
    - trade count, USDC volume, unique markets
    - win rate (fraction of positions that resolved in their favor)
    - realized PnL and ROI
    - avg / median trade size
    - sharpe-like (pnl_mean / pnl_std) across per-market outcomes
    - avg holding time (buy → sell/resolution)
    - early-entry score (how early in a market's life they entered)
    - contrarian score (trade price vs. market mid at time of trade)
"""

from __future__ import annotations

import logging
import sqlite3
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
    condition_id: str | None
    token_id: str | None
    side: str | None
    price: float
    size: float
    usdc: float


def compute_trader_metrics(cfg: Config, db: Database) -> int:
    """Recompute trader_metrics from scratch. Returns rows written."""
    min_trades = cfg.analyzer.min_trades_for_ranking
    min_volume = cfg.analyzer.min_volume_usdc

    # Gather every taker fill, grouped per address.
    trades_by_addr: dict[str, list[_Fill]] = defaultdict(list)
    for row in db.conn.execute(
        """
        SELECT taker, ts, condition_id, token_id, side, price, size, usdc_size
        FROM trades WHERE taker IS NOT NULL
        """
    ):
        trades_by_addr[row["taker"]].append(
            _Fill(
                ts=row["ts"],
                condition_id=row["condition_id"],
                token_id=row["token_id"],
                side=row["side"],
                price=float(row["price"] or 0),
                size=float(row["size"] or 0),
                usdc=float(row["usdc_size"] or 0),
            )
        )

    market_start_ts = _market_first_trade_ts(db)
    winner_by_token = _winner_by_token(db)
    now = int(time.time())

    rows_written = 0
    with db.tx() as cur:
        for addr, fills in trades_by_addr.items():
            if len(fills) < min_trades:
                continue
            volume = sum(f.usdc for f in fills)
            if volume < min_volume:
                continue

            pnl_per_market, holding_secs = _realized_pnl_by_market(fills, winner_by_token)
            realized = sum(pnl_per_market.values())
            roi = realized / volume if volume > 0 else 0.0

            wins = sum(1 for v in pnl_per_market.values() if v > 0)
            losses = sum(1 for v in pnl_per_market.values() if v < 0)
            win_rate = wins / (wins + losses) if (wins + losses) else 0.0

            sizes = [f.usdc for f in fills if f.usdc > 0]
            avg_size = sum(sizes) / len(sizes) if sizes else 0.0
            med_size = stats.median(sizes) if sizes else 0.0

            sample = list(pnl_per_market.values())
            if len(sample) > 1:
                mean = stats.fmean(sample)
                sd = stats.pstdev(sample) or 1e-9
                sharpe_like = mean / sd
            else:
                sharpe_like = 0.0

            max_dd = _max_drawdown(fills, winner_by_token)
            markets_traded = len({f.condition_id for f in fills if f.condition_id})
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
                    realized,
                    roi,
                    win_rate,
                    avg_size,
                    med_size,
                    max_dd,
                    sharpe_like,
                    markets_traded,
                    avg_hold,
                    early_score,
                    0.0,  # contrarian_score — filled by patterns.py when mid-price data is available
                    None,
                ),
            )
            rows_written += 1
    log.info("computed metrics for %d traders", rows_written)
    return rows_written


def _market_first_trade_ts(db: Database) -> dict[str, int]:
    return {
        r["condition_id"]: r["first_ts"]
        for r in db.conn.execute(
            "SELECT condition_id, MIN(ts) AS first_ts FROM trades "
            "WHERE condition_id IS NOT NULL GROUP BY condition_id"
        )
    }


def _winner_by_token(db: Database) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in db.conn.execute(
        "SELECT token_id, winner FROM market_tokens WHERE winner IS NOT NULL"
    ):
        out[r["token_id"]] = int(r["winner"])
    return out


def _realized_pnl_by_market(
    fills: list[_Fill], winner_by_token: dict[str, int]
) -> tuple[dict[str, float], list[float]]:
    """Approximate realized PnL using token-level FIFO.

    For each token_id, walk fills in chronological order. Buys accumulate
    cost, sells net against FIFO cost. At end: if the token resolved, any
    remaining shares pay out at {1 if winner else 0}. Markets still open
    and with unrealized exposure contribute 0 here — realized PnL only.
    """
    pnl: dict[str, float] = defaultdict(float)
    holding_secs: list[float] = []
    by_token: dict[str, list[_Fill]] = defaultdict(list)
    for f in fills:
        if f.token_id:
            by_token[f.token_id].append(f)
    for token_id, tfills in by_token.items():
        tfills.sort(key=lambda x: x.ts)
        inventory: list[tuple[float, float, int]] = []  # (size, price, ts)
        for f in tfills:
            if f.side == "BUY":
                inventory.append((f.size, f.price, f.ts))
            elif f.side == "SELL":
                remaining = f.size
                while remaining > 0 and inventory:
                    lot_size, lot_price, lot_ts = inventory[0]
                    take = min(lot_size, remaining)
                    market_id = tfills[0].condition_id or token_id
                    pnl[market_id] += take * (f.price - lot_price)
                    holding_secs.append(max(0, f.ts - lot_ts))
                    remaining -= take
                    if take >= lot_size:
                        inventory.pop(0)
                    else:
                        inventory[0] = (lot_size - take, lot_price, lot_ts)
        # Settle remaining inventory at resolution price if known
        winner = winner_by_token.get(token_id)
        if winner is not None and inventory:
            settle_price = 1.0 if winner else 0.0
            market_id = tfills[0].condition_id or token_id
            for lot_size, lot_price, _ in inventory:
                pnl[market_id] += lot_size * (settle_price - lot_price)
    return pnl, holding_secs


def _max_drawdown(fills: list[_Fill], winner_by_token: dict[str, int]) -> float:
    """Running PnL over time → max peak-to-trough drop."""
    pnl_per_market, _ = _realized_pnl_by_market(sorted(fills, key=lambda f: f.ts), winner_by_token)
    # Approximate timeline: accumulate each market's PnL at the time of its last fill.
    events: list[tuple[int, float]] = []
    last_ts_by_market: dict[str, int] = {}
    for f in fills:
        if f.condition_id:
            last_ts_by_market[f.condition_id] = max(
                last_ts_by_market.get(f.condition_id, 0), f.ts
            )
    for market_id, p in pnl_per_market.items():
        events.append((last_ts_by_market.get(market_id, 0), p))
    events.sort()
    peak = 0.0
    cum = 0.0
    max_dd = 0.0
    for _, p in events:
        cum += p
        peak = max(peak, cum)
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd
    return max_dd


def _early_entry_score(fills: list[_Fill], market_start_ts: dict[str, int]) -> float:
    """0 = entered first second; 1 = entered much later. Lower is better.

    Measured relative to the first observed trade of each market. If we
    don't know the market start, the fill is ignored.
    """
    ratios: list[float] = []
    by_market: dict[str, list[_Fill]] = defaultdict(list)
    for f in fills:
        if f.condition_id:
            by_market[f.condition_id].append(f)
    for cid, mfills in by_market.items():
        start = market_start_ts.get(cid)
        if start is None:
            continue
        my_first = min(f.ts for f in mfills)
        # Span: use now - start as denominator; clip to 1 day minimum
        span = max(int(time.time()) - start, 86400)
        ratios.append(max(0.0, min(1.0, (my_first - start) / span)))
    if not ratios:
        return 1.0
    return _raw_mean(ratios)


def _raw_mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0
