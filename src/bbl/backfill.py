"""Backfill — pull historical trades for a set of wallets.

The /trades and /activity endpoints support offset-based pagination. We
walk back until either we hit a maximum age, a max page count, or the
endpoint returns fewer than `page_size` rows (= last page).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

from bbl.clients import DataClient
from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)


async def backfill_wallet(
    cfg: Config,
    db: Database,
    address: str,
    *,
    max_pages: int = 200,
    page_size: int = 500,
    since_ts: int | None = None,
) -> int:
    """Paginate /activity for one wallet. Returns rows inserted."""
    address = address.lower()
    inserted = 0
    async with DataClient(cfg.api) as data:
        for page in range(max_pages):
            offset = page * page_size
            try:
                trades = await data.get_activity(
                    user=address, limit=page_size, offset=offset
                )
            except Exception as e:
                log.warning("activity page %d for %s failed: %s", page, address, e)
                break
            if not trades:
                break
            inserted += db.insert_trades(trades)
            # Stop early if the oldest row on this page is already older than
            # our cutoff — saves a ton of API calls when topping up.
            if since_ts is not None:
                oldest = min(_row_ts(t) for t in trades if _row_ts(t))
                if oldest < since_ts:
                    log.debug("hit since_ts cutoff for %s at page %d", address, page)
                    break
            if len(trades) < page_size:
                break
    log.info("backfill %s: %d rows", address, inserted)
    return inserted


async def backfill_wallets(
    cfg: Config,
    db: Database,
    addresses: Iterable[str],
    *,
    concurrency: int = 4,
    **kwargs,
) -> int:
    sem = asyncio.Semaphore(concurrency)
    total = 0

    async def _one(addr: str) -> int:
        async with sem:
            return await backfill_wallet(cfg, db, addr, **kwargs)

    results = await asyncio.gather(
        *[_one(a) for a in addresses], return_exceptions=True
    )
    for r in results:
        if isinstance(r, Exception):
            log.warning("backfill task failed: %s", r)
        else:
            total += r
    return total


async def backfill_top_traders(
    cfg: Config,
    db: Database,
    *,
    limit: int = 200,
    metric: str = "profit",
    window: str = "all",
    since_days: int | None = 90,
) -> int:
    """Backfill activity for the top N wallets on a given leaderboard."""
    rows = db.conn.execute(
        """
        SELECT DISTINCT address FROM leaderboard_snapshots
         WHERE window = ? AND metric = ?
           AND ts = (SELECT MAX(ts) FROM leaderboard_snapshots WHERE window=? AND metric=?)
         ORDER BY rank ASC
         LIMIT ?
        """,
        (window, metric, window, metric, limit),
    ).fetchall()
    addrs = [r[0] for r in rows]
    if not addrs:
        log.info("no leaderboard data yet — run leaderboard collector first")
        return 0
    since = int(time.time() - since_days * 86400) if since_days else None
    return await backfill_wallets(cfg, db, addrs, since_ts=since)


def _row_ts(t: dict) -> int | None:
    raw = t.get("timestamp") or t.get("ts") or t.get("time")
    if raw is None:
        return None
    try:
        v = int(float(raw))
        return v // 1000 if v > 10_000_000_000 else v
    except (TypeError, ValueError):
        return None
