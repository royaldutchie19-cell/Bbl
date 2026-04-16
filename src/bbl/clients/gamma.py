"""Gamma API — market & event metadata.

Gamma is the read-only metadata surface: markets, events, tags, outcomes.
Docs: https://docs.polymarket.com/developers/gamma-markets-api
"""

from __future__ import annotations

from typing import Any

from bbl.clients.base import BaseClient
from bbl.config import APIConfig


class GammaClient(BaseClient):
    def __init__(self, cfg: APIConfig):
        super().__init__(cfg, base_url=cfg.gamma_base)

    async def list_markets(
        self,
        *,
        active: bool | None = True,
        closed: bool | None = False,
        archived: bool | None = False,
        limit: int = 500,
        offset: int = 0,
        order: str = "volumeNum",
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
        """Return a page of markets. Use iter_markets() for full pagination."""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "order": order,
            "ascending": str(ascending).lower(),
        }
        if active is not None:
            params["active"] = str(active).lower()
        if closed is not None:
            params["closed"] = str(closed).lower()
        if archived is not None:
            params["archived"] = str(archived).lower()
        data = await self.get("/markets", **params)
        return data if isinstance(data, list) else data.get("data", [])

    async def iter_markets(
        self, *, page_size: int = 500, **filters: Any
    ) -> list[dict[str, Any]]:
        """Paginate through all markets matching filters."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self.list_markets(limit=page_size, offset=offset, **filters)
            if not page:
                break
            out.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        return out

    async def get_market(self, market_id: str | int) -> dict[str, Any] | None:
        return await self.get(f"/markets/{market_id}")

    async def list_events(
        self, *, active: bool = True, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        data = await self.get(
            "/events", active=str(active).lower(), limit=limit, offset=offset
        )
        return data if isinstance(data, list) else data.get("data", [])
