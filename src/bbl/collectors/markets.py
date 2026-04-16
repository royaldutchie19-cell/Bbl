"""Markets collector — keeps the market catalog fresh via Gamma + CLOB.

Gamma has the rich metadata (question, category, event). CLOB has the
canonical tokens and min tick size. We merge both per market.
"""

from __future__ import annotations

from bbl.clients import ClobClient, GammaClient
from bbl.collectors.base import BaseCollector, CollectorResult


class MarketsCollector(BaseCollector):
    name = "markets"

    @property
    def interval_s(self) -> int:
        return self.cfg.collector.markets_interval_s

    async def tick(self) -> CollectorResult:
        # Gamma: active + closed separately so we also keep recently resolved
        # markets around for analyzer work.
        async with GammaClient(self.cfg.api) as gamma:
            active = await gamma.iter_markets(active=True, closed=False, archived=False)
        async with ClobClient(self.cfg.api) as clob:
            clob_rows = await clob.iter_markets()

        by_cond: dict[str, dict] = {}
        for m in active:
            cid = m.get("conditionId") or m.get("condition_id")
            if cid:
                by_cond[cid] = dict(m)
        for m in clob_rows:
            cid = m.get("condition_id") or m.get("conditionId")
            if not cid:
                continue
            merged = by_cond.setdefault(cid, {})
            # CLOB gives us tokens[] with token_id + outcome + winner
            merged.setdefault("conditionId", cid)
            if "tokens" in m:
                merged["clobTokenIds"] = m["tokens"]
            for k in ("minimum_tick_size", "minTickSize", "question"):
                if k in m and k not in merged:
                    merged[k] = m[k]
            if "minimum_tick_size" in m:
                merged["minimumTickSize"] = m["minimum_tick_size"]

        rows = self.db.upsert_markets(by_cond.values())
        return CollectorResult(rows_written=rows, note=f"{len(by_cond)} markets merged")
