"""Orderbook collector — snapshot full books for top markets.

Books are bigger than midpoints, so we poll fewer markets but store depth.
We only keep the most recent N levels per side to bound row size.
"""

from __future__ import annotations

import time

from bbl.clients import ClobClient
from bbl.collectors.base import BaseCollector, CollectorResult

MAX_LEVELS = 50


class OrderbookCollector(BaseCollector):
    name = "orderbooks"

    @property
    def interval_s(self) -> int:
        return self.cfg.collector.orderbook_interval_s

    async def tick(self) -> CollectorResult:
        # Orderbooks are expensive; target top ~50 markets by default.
        limit = min(self.cfg.collector.top_markets_limit, 100)
        token_ids = self.db.active_token_ids(limit=limit)
        if not token_ids:
            return CollectorResult(rows_written=0, note="no active tokens")

        now = int(time.time())
        rows = 0
        async with ClobClient(self.cfg.api) as clob:
            for chunk in _chunks(token_ids, 50):
                try:
                    books = await clob.get_orderbooks(chunk)
                except Exception:
                    continue
                for book in books:
                    tid = book.get("asset_id") or book.get("token_id")
                    if not tid:
                        continue
                    bids = _levels(book.get("bids"))
                    asks = _levels(book.get("asks"))
                    self.db.insert_orderbook_snapshot(
                        ts=now,
                        token_id=tid,
                        bids=bids,
                        asks=asks,
                    )
                    rows += 1
        return CollectorResult(rows_written=rows)


def _levels(raw) -> list[list[float]]:
    if not raw:
        return []
    out = []
    for lvl in raw[:MAX_LEVELS]:
        if isinstance(lvl, dict):
            p = lvl.get("price")
            s = lvl.get("size")
        elif isinstance(lvl, (list, tuple)) and len(lvl) >= 2:
            p, s = lvl[0], lvl[1]
        else:
            continue
        try:
            out.append([float(p), float(s)])
        except (TypeError, ValueError):
            continue
    return out


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i : i + n]
