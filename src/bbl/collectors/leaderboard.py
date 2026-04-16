"""Leaderboard collector — snapshots top traders by profit and volume.

We snapshot all four windows (day/week/month/all) × both metrics (profit,
volume) each tick so we can study how traders move up and down.
"""

from __future__ import annotations

import asyncio
import logging
import time

from bbl.clients import DataClient
from bbl.collectors.base import BaseCollector, CollectorResult

log = logging.getLogger(__name__)

WINDOWS = ("day", "week", "month", "all")
METRICS = ("profit", "volume")


class LeaderboardCollector(BaseCollector):
    name = "leaderboard"

    @property
    def interval_s(self) -> int:
        return self.cfg.collector.leaderboard_interval_s

    async def tick(self) -> CollectorResult:
        now = int(time.time())
        rows = 0
        async with DataClient(self.cfg.api) as data:
            tasks = []
            for window in WINDOWS:
                for metric in METRICS:
                    tasks.append(
                        self._snapshot(data, now, window, metric)
                    )
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    log.warning("leaderboard snapshot failed: %s", r)
                    continue
                rows += r

            # Pull profile stats for everyone on the "all-time profit" board.
            try:
                top = await data.get_leaderboard(window="all", metric="profit", limit=200)
                # Upserting with whatever the leaderboard payload gives us;
                # per-user endpoint enriches display_name + bio for the top of
                # the list only (to keep this tick bounded).
                rows += self.db.upsert_traders(top)
                for entry in top[:50]:
                    addr = (entry.get("proxyWallet") or entry.get("address") or "").lower()
                    if not addr:
                        continue
                    try:
                        profile = await data.get_user(addr)
                        if profile:
                            profile.setdefault("proxyWallet", addr)
                            self.db.upsert_traders([profile])
                    except Exception:
                        pass
            except Exception as e:
                log.warning("leaderboard enrichment failed: %s", e)
        return CollectorResult(rows_written=rows)

    async def _snapshot(
        self, data: DataClient, ts: int, window: str, metric: str
    ) -> int:
        entries = await data.get_leaderboard(window=window, metric=metric, limit=500)
        return self.db.insert_leaderboard_snapshot(
            ts=ts, window=window, metric=metric, entries=entries
        )
