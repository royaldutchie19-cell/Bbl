"""Resolutions collector — backfill winner flags on resolved markets.

Markets close, then resolve. The CLOB exposes the winner per token via the
`winner` field in `/markets`. We walk closed markets without winners in
our DB and re-fetch them so PnL settles correctly.
"""

from __future__ import annotations

import logging

from bbl.clients import ClobClient
from bbl.collectors.base import BaseCollector, CollectorResult

log = logging.getLogger(__name__)


class ResolutionsCollector(BaseCollector):
    name = "resolutions"

    @property
    def interval_s(self) -> int:
        # Resolutions move slowly; re-check less often than market metadata.
        return max(self.cfg.collector.markets_interval_s * 4, 1800)

    async def tick(self) -> CollectorResult:
        # Find closed markets where we still have unresolved tokens.
        rows = self.db.conn.execute(
            """
            SELECT DISTINCT m.condition_id
              FROM markets m
              JOIN market_tokens mt ON mt.condition_id = m.condition_id
             WHERE m.closed = 1 AND mt.winner IS NULL
             ORDER BY m.last_seen_ts DESC
             LIMIT 500
            """
        ).fetchall()
        if not rows:
            return CollectorResult(rows_written=0, note="no unresolved markets")

        async with ClobClient(self.cfg.api) as clob:
            # Re-fetch the full CLOB market list; cheaper than per-market lookups
            # and gives us all winners in one walk.
            clob_markets = await clob.iter_markets()
        unresolved = {r["condition_id"] for r in rows}
        winners_set = 0
        with self.db.tx() as cur:
            for m in clob_markets:
                cid = m.get("condition_id") or m.get("conditionId")
                if cid not in unresolved:
                    continue
                for tok in m.get("tokens", []) or []:
                    tid = tok.get("token_id") or tok.get("tokenId")
                    if not tid or "winner" not in tok:
                        continue
                    cur.execute(
                        "UPDATE market_tokens SET winner = ? WHERE token_id = ?",
                        (int(bool(tok["winner"])), str(tid)),
                    )
                    winners_set += cur.rowcount
        return CollectorResult(rows_written=winners_set)
