"""Shared HTTP client: async httpx with rate limiting + retry/backoff."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from bbl.config import APIConfig

log = logging.getLogger(__name__)


class RateLimiter:
    """Simple token-bucket rate limiter for async contexts.

    We're a polite consumer of a public API — this keeps us from tripping
    rate limits and getting temp-banned.
    """

    def __init__(self, rps: float, burst: int | None = None):
        self.rps = rps
        self.capacity = burst or max(1, int(rps))
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._last = now
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rps)
            if self._tokens < 1.0:
                wait = (1.0 - self._tokens) / self.rps
                await asyncio.sleep(wait)
                self._tokens = 0.0
            else:
                self._tokens -= 1.0


class BaseClient:
    """Async HTTP client with rate limiting, retries, and JSON helpers.

    Use as an async context manager:

        async with ClobClient(api_cfg) as clob:
            book = await clob.get_orderbook(token_id)
    """

    base_url: str = ""

    def __init__(self, api_cfg: APIConfig, base_url: str | None = None):
        self.cfg = api_cfg
        if base_url:
            self.base_url = base_url
        self._client: httpx.AsyncClient | None = None
        self._limiter = RateLimiter(api_cfg.rps)
        self._sem = asyncio.Semaphore(api_cfg.max_concurrency)

    async def __aenter__(self) -> BaseClient:
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.cfg.request_timeout_s,
            headers={"User-Agent": self.cfg.user_agent, "Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
        self._client = None

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        assert self._client is not None, "use as async context manager"
        # Retry on transient failures; surface 4xx other than 429 immediately.
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(5),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.ReadTimeout, _TransientHTTPError)
            ),
            reraise=True,
        ):
            with attempt:
                await self._limiter.acquire()
                async with self._sem:
                    resp = await self._client.request(method, path, **kwargs)
                if resp.status_code == 429:
                    # Respect Retry-After if present, else backoff via tenacity.
                    retry_after = float(resp.headers.get("Retry-After", "2"))
                    log.warning("rate limited on %s, sleeping %.1fs", path, retry_after)
                    await asyncio.sleep(retry_after)
                    raise _TransientHTTPError("429")
                if 500 <= resp.status_code < 600:
                    raise _TransientHTTPError(f"{resp.status_code}")
                resp.raise_for_status()
                if not resp.content:
                    return None
                ctype = resp.headers.get("content-type", "")
                if "json" in ctype:
                    return resp.json()
                return resp.text
        return None  # unreachable

    async def get(self, path: str, **params: Any) -> Any:
        return await self._request("GET", path, params=params or None)

    async def post(self, path: str, json: Any | None = None, **params: Any) -> Any:
        return await self._request("POST", path, json=json, params=params or None)


class _TransientHTTPError(Exception):
    """Raised for 429/5xx so tenacity retries them."""
