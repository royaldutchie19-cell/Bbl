"""Collector base class — one tick = fetch + persist + log."""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)


@dataclass
class CollectorResult:
    rows_written: int = 0
    note: str | None = None


class BaseCollector(ABC):
    """Subclass and implement `interval_s` + `tick()`.

    Each collector owns its own pacing. `run_forever()` loops until cancelled;
    `run_once()` performs a single tick (useful for CLI + tests).
    """

    name: str = "base"

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db

    @property
    @abstractmethod
    def interval_s(self) -> int: ...

    @abstractmethod
    async def tick(self) -> CollectorResult: ...

    async def run_once(self) -> CollectorResult:
        started = int(time.time())
        try:
            result = await self.tick()
            self.db.log_run(
                collector=self.name,
                started_ts=started,
                finished_ts=int(time.time()),
                status="ok",
                rows_written=result.rows_written,
            )
            return result
        except Exception as e:
            log.exception("%s collector failed", self.name)
            self.db.log_run(
                collector=self.name,
                started_ts=started,
                finished_ts=int(time.time()),
                status="error",
                error=str(e),
            )
            raise

    async def run_forever(self) -> None:
        log.info("%s collector starting (interval=%ss)", self.name, self.interval_s)
        while True:
            t0 = time.monotonic()
            try:
                res = await self.run_once()
                log.info("%s tick → %d rows", self.name, res.rows_written)
            except Exception as e:
                # error already logged by run_once; sleep and continue
                log.warning("%s tick raised %s — continuing", self.name, e)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(1, self.interval_s - elapsed))
