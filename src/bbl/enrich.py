"""Per-wallet enrichment — deep-pull the slow data-api endpoints.

Call this after you have a leaderboard + metrics, to fill in:
  - current positions
  - portfolio-value time series
  - pnl time series
  - rewards / earnings
  - volume stats

Not meant to run on every tick — it's N requests per wallet. Use
`bbl enrich top --limit 50` once a day, or `bbl enrich wallet 0x...`
on-demand.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable

from bbl.clients import ClobClient, DataClient
from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)


async def enrich_wallet(
    cfg: Config,
    db: Database,
    address: str,
    *,
    positions: bool = True,
    pnl: bool = True,
    portfolio: bool = True,
    rewards: bool = True,
) -> dict[str, int]:
    """Pull everything we can about a single wallet. Returns rows-per-source."""
    addr = address.lower()
    stats = {"positions": 0, "pnl": 0, "portfolio": 0, "rewards": 0, "clob_rewards": 0}
    now = int(time.time())

    async with DataClient(cfg.api) as data:
        if positions:
            try:
                pos = await data.get_positions(user=addr)
                stats["positions"] = db.insert_positions(now, addr, pos)
            except Exception as e:
                log.warning("positions for %s failed: %s", addr, e)

        if portfolio:
            try:
                series = await data.get_portfolio_value(user=addr, interval="all", fidelity=1440)
                stats["portfolio"] = db.insert_user_value_series(addr, series or [])
            except Exception as e:
                log.warning("portfolio-value for %s failed: %s", addr, e)

        if pnl:
            try:
                series = await data.get_pnl(user=addr, interval="all", fidelity=1440)
                stats["pnl"] = db.insert_user_pnl_series(addr, series or [])
            except Exception as e:
                log.warning("pnl for %s failed: %s", addr, e)

        if rewards:
            try:
                r = await data.get_user_rewards(user=addr)
                if r:
                    entries = r if isinstance(r, list) else r.get("data", []) or [r]
                    stats["rewards"] = db.insert_user_rewards(addr, entries)
            except Exception as e:
                log.warning("data rewards for %s failed: %s", addr, e)

    # CLOB also exposes a rewards endpoint — try it too (they report different
    # things: data-api = historical earnings, CLOB = current-epoch accrual).
    async with ClobClient(cfg.api) as clob:
        try:
            r2 = await clob.get_user_rewards(address=addr)
            if r2:
                entries = r2 if isinstance(r2, list) else r2.get("data", []) or [r2]
                stats["clob_rewards"] = db.insert_user_rewards(addr, entries)
        except Exception as e:
            log.debug("clob rewards for %s skipped: %s", addr, e)

    log.info("enrich %s: %s", addr, stats)
    return stats


async def enrich_wallets(
    cfg: Config,
    db: Database,
    addresses: Iterable[str],
    *,
    concurrency: int = 4,
    **kwargs,
) -> dict[str, int]:
    totals = {"positions": 0, "pnl": 0, "portfolio": 0, "rewards": 0, "clob_rewards": 0}
    sem = asyncio.Semaphore(concurrency)

    async def _one(addr: str) -> dict[str, int]:
        async with sem:
            return await enrich_wallet(cfg, db, addr, **kwargs)

    results = await asyncio.gather(
        *[_one(a) for a in addresses], return_exceptions=True
    )
    for r in results:
        if isinstance(r, Exception):
            log.warning("enrich task failed: %s", r)
            continue
        for k, v in r.items():
            totals[k] = totals.get(k, 0) + v
    return totals


async def enrich_top_traders(
    cfg: Config,
    db: Database,
    *,
    limit: int = 50,
    metric: str = "profit",
    window: str = "all",
) -> dict[str, int]:
    rows = db.conn.execute(
        """
        SELECT DISTINCT address FROM leaderboard_snapshots
         WHERE window = ? AND metric = ?
           AND ts = (SELECT MAX(ts) FROM leaderboard_snapshots WHERE window=? AND metric=?)
         ORDER BY rank ASC LIMIT ?
        """,
        (window, metric, window, metric, limit),
    ).fetchall()
    addrs = [r[0] for r in rows]
    if not addrs:
        log.info("no leaderboard entries yet")
        return {}
    return await enrich_wallets(cfg, db, addrs)


async def enrich_market_holders(
    cfg: Config,
    db: Database,
    *,
    limit: int = 50,
) -> int:
    """Snapshot top holders for the N highest-volume active markets."""
    markets = db.active_markets(limit=limit)
    if not markets:
        return 0
    now = int(time.time())
    rows = 0
    async with DataClient(cfg.api) as data:
        for m in markets:
            cid = m["condition_id"]
            try:
                holders = await data.get_holders(cid, limit=100)
                rows += db.insert_market_holders(cid, now, holders)
            except Exception as e:
                log.warning("holders for %s failed: %s", cid[:12], e)
    return rows
