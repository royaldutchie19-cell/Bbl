"""Prices collector — batch midpoint snapshots for top N active tokens."""

from __future__ import annotations

import asyncio
import time

from bbl.clients import ClobClient
from bbl.collectors.base import BaseCollector, CollectorResult


class PricesCollector(BaseCollector):
    name = "prices"

    @property
    def interval_s(self) -> int:
        return self.cfg.collector.prices_interval_s

    async def tick(self) -> CollectorResult:
        token_ids = self.db.active_token_ids(limit=self.cfg.collector.top_markets_limit)
        if not token_ids:
            return CollectorResult(rows_written=0, note="no active tokens")
        now = int(time.time())
        rows = 0
        async with ClobClient(self.cfg.api) as clob:
            # Midpoints in batches — endpoint caps around 100 ids per call.
            for chunk in _chunks(token_ids, 100):
                try:
                    mids = await clob.get_midpoints(chunk)
                except Exception:
                    mids = {}
                # Spreads in parallel, best-effort per token.
                spread_tasks = [clob.get_spread(t) for t in chunk]
                spreads = await asyncio.gather(*spread_tasks, return_exceptions=True)
                for t, spread_res in zip(chunk, spreads):
                    mid = _f((mids or {}).get(t))
                    best_bid = best_ask = None
                    if isinstance(spread_res, dict):
                        # /spread returns {"spread": "0.02"} — we can only
                        # recover bid/ask if midpoint is known.
                        sp = _f(spread_res.get("spread"))
                        if sp is not None and mid is not None:
                            best_bid = round(mid - sp / 2, 6)
                            best_ask = round(mid + sp / 2, 6)
                    self.db.insert_price_snapshot(
                        ts=now,
                        token_id=t,
                        mid=mid,
                        best_bid=best_bid,
                        best_ask=best_ask,
                    )
                    rows += 1
        return CollectorResult(rows_written=rows)


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def _f(x):
    if x is None:
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
