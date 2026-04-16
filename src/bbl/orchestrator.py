"""Orchestrator — run N collectors concurrently on independent schedules.

Each collector is a `run_forever()` coroutine. asyncio gathers them under a
single event loop and lets them step on the same DB connection sequentially
inside each individual tick; inter-collector concurrency happens at the
network layer where it matters.

Supports running 1..N collectors from the CLI, so you can have three (or
more) parallel "runs" — e.g. markets + trades + leaderboard — without
needing extra processes.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Iterable

from bbl.collectors import ALL_COLLECTORS, BaseCollector
from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)


def build_collectors(cfg: Config, db: Database, names: Iterable[str]) -> list[BaseCollector]:
    out: list[BaseCollector] = []
    for n in names:
        cls = ALL_COLLECTORS.get(n)
        if not cls:
            raise ValueError(f"unknown collector: {n} (have {list(ALL_COLLECTORS)})")
        out.append(cls(cfg, db))
    return out


async def run_collectors_forever(collectors: list[BaseCollector]) -> None:
    stop = asyncio.Event()

    def _sigterm() -> None:
        log.info("shutdown signal received")
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _sigterm)
        except NotImplementedError:
            # windows: fall through, KeyboardInterrupt will still propagate
            pass

    tasks = [asyncio.create_task(c.run_forever(), name=c.name) for c in collectors]
    stop_task = asyncio.create_task(stop.wait(), name="stop")
    done, pending = await asyncio.wait(
        [*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    for t in done:
        if t is stop_task:
            continue
        if exc := t.exception():
            log.error("collector %s crashed: %s", t.get_name(), exc)
    # drain cancellations
    for t in pending:
        try:
            await t
        except asyncio.CancelledError:
            pass
