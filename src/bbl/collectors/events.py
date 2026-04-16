"""Events collector — keeps the Gamma event catalog + tag list fresh."""

from __future__ import annotations

from bbl.clients import GammaClient
from bbl.collectors.base import BaseCollector, CollectorResult


class EventsCollector(BaseCollector):
    name = "events"

    @property
    def interval_s(self) -> int:
        # Events change about as slowly as markets.
        return self.cfg.collector.markets_interval_s

    async def tick(self) -> CollectorResult:
        async with GammaClient(self.cfg.api) as gamma:
            active = await gamma.iter_events(active=True)
            closed = await gamma.iter_events(active=False, closed=True)
            try:
                tags = await gamma.list_tags(limit=500)
            except Exception:
                tags = []
        rows_e = self.db.upsert_events([*active, *closed])
        rows_t = self.db.upsert_tags(tags) if tags else 0
        return CollectorResult(
            rows_written=rows_e + rows_t,
            note=f"events={rows_e} tags={rows_t}",
        )
