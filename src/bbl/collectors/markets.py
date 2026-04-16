"""Markets collector — keeps the market catalog fresh via Gamma + CLOB.

Gamma has the rich metadata (question, category, event). CLOB has the
canonical tokens, min tick size, and the authoritative active/closed flags.
We merge both per market.

We deliberately fetch *both* active and recently-closed markets, because
the analyzer needs resolved markets (with winner flags on tokens) to
settle PnL.
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
        async with GammaClient(self.cfg.api) as gamma:
            active = await gamma.iter_markets(active=True, closed=False, archived=False)
            # Recently closed — needed so winner flags propagate. We don't
            # need *all* historical closed markets every tick; the
            # resolutions collector handles deeper backfill.
            closed = await gamma.iter_markets(
                active=None, closed=True, archived=False, page_size=500
            )
        async with ClobClient(self.cfg.api) as clob:
            clob_rows = await clob.iter_markets()

        by_cond: dict[str, dict] = {}
        for m in (*active, *closed):
            cid = m.get("conditionId") or m.get("condition_id")
            if cid:
                by_cond[cid] = dict(m)

        for m in clob_rows:
            cid = m.get("condition_id") or m.get("conditionId")
            if not cid:
                continue
            merged = by_cond.setdefault(cid, {"conditionId": cid})
            # CLOB is authoritative for these flags + tokens
            if "tokens" in m:
                merged["clobTokenIds"] = m["tokens"]
            if "minimum_tick_size" in m:
                merged["minimumTickSize"] = m["minimum_tick_size"]
            for src, dst in (
                ("active", "active"),
                ("closed", "closed"),
                ("archived", "archived"),
                ("question", "question"),
                ("question_id", "questionID"),
            ):
                if src in m and m[src] is not None and dst not in merged:
                    merged[dst] = m[src]
            # Always let CLOB override the lifecycle booleans — they're
            # the on-chain source of truth.
            for flag in ("active", "closed", "archived"):
                if flag in m and m[flag] is not None:
                    merged[flag] = m[flag]

        rows = self.db.upsert_markets(by_cond.values())
        return CollectorResult(
            rows_written=rows,
            note=f"{len(active)} active, {len(closed)} closed, {len(clob_rows)} clob",
        )
