"""Price-history collector — pulls OHLC candles for top markets.

Unlike the live `prices` collector (which takes midpoint snapshots at
runtime), this one asks CLOB for its server-side history. Much denser +
covers time before we started collecting. Good for backtest and
"did they trade before the move?" analysis.
"""

from __future__ import annotations

import asyncio
import logging

from bbl.clients import ClobClient
from bbl.collectors.base import BaseCollector, CollectorResult

log = logging.getLogger(__name__)

DEFAULT_FIDELITY = 60  # 1h candles


class PriceHistoryCollector(BaseCollector):
    name = "price_history"

    @property
    def interval_s(self) -> int:
        # Candles don't change retroactively; once an hour is plenty.
        return max(self.cfg.collector.prices_interval_s * 60, 1800)

    async def tick(self) -> CollectorResult:
        token_ids = self.db.active_token_ids(
            limit=min(self.cfg.collector.top_markets_limit, 50)
        )
        if not token_ids:
            return CollectorResult(rows_written=0, note="no active tokens")

        rows = 0
        sem = asyncio.Semaphore(4)
        async with ClobClient(self.cfg.api) as clob:
            async def _fetch(tid: str) -> int:
                async with sem:
                    try:
                        candles = await clob.get_price_history(
                            token_id=tid,
                            interval="1w",
                            fidelity=DEFAULT_FIDELITY,
                        )
                    except Exception as e:
                        log.warning("price-history %s failed: %s", tid[:12], e)
                        return 0
                    if not candles:
                        return 0
                    return self.db.insert_price_history(
                        token_id=tid, fidelity=DEFAULT_FIDELITY, candles=candles
                    )

            results = await asyncio.gather(*[_fetch(t) for t in token_ids])
            rows = sum(results)
        return CollectorResult(rows_written=rows)
