"""Trades collector — pulls recent fills from data-api.

Strategy: each tick we fetch the latest page of global trades and dedupe on
trade_id. For tracked (watchlist) wallets we also pull per-user activity
so we don't miss small trades that got bumped off the global feed.
"""

from __future__ import annotations

import logging

from bbl.clients import DataClient
from bbl.collectors.base import BaseCollector, CollectorResult

log = logging.getLogger(__name__)


class TradesCollector(BaseCollector):
    name = "trades"

    @property
    def interval_s(self) -> int:
        return self.cfg.collector.trades_interval_s

    async def tick(self) -> CollectorResult:
        rows = 0
        async with DataClient(self.cfg.api) as data:
            # Global recent trades — bounded page.
            try:
                recent = await data.get_trades(limit=500, taker_only=True)
                rows += self.db.insert_trades(recent)
            except Exception as e:
                log.warning("global trades fetch failed: %s", e)

            # Watchlist wallets — make sure we have full history recently.
            watch_addrs = [
                r[0]
                for r in self.db.conn.execute(
                    "SELECT address FROM watchlist WHERE active=1"
                ).fetchall()
            ]
            for addr in watch_addrs:
                try:
                    user_trades = await data.get_activity(user=addr, limit=200)
                    rows += self.db.insert_trades(user_trades)
                except Exception as e:
                    log.warning("activity fetch failed for %s: %s", addr, e)
        return CollectorResult(rows_written=rows)
