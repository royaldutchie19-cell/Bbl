"""Gamma API — market & event metadata, tags, series, comments.

Gamma is the read-only metadata surface.
Docs: https://docs.polymarket.com/developers/gamma-markets-api

Endpoints covered:
  /markets            /markets/{id_or_slug}
  /events             /events/{id_or_slug}
  /tags               /tags/{id_or_slug}
  /series             /series/{id_or_slug}
  /comments           /comments/{id}
"""

from __future__ import annotations

from typing import Any

from bbl.clients.base import BaseClient
from bbl.config import APIConfig


class GammaClient(BaseClient):
    def __init__(self, cfg: APIConfig):
        super().__init__(cfg, base_url=cfg.gamma_base)

    # ---------------------------------------------------------- markets

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
        tag_id: int | None = None,
        event_id: int | None = None,
    ) -> list[dict[str, Any]]:
        """One page of markets. Use iter_markets() for full pagination."""
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
        if tag_id is not None:
            params["tag_id"] = tag_id
        if event_id is not None:
            params["event_id"] = event_id
        data = await self.get("/markets", **params)
        return data if isinstance(data, list) else data.get("data", [])

    async def iter_markets(
        self, *, page_size: int = 500, max_rows: int = 0, **filters: Any
    ) -> list[dict[str, Any]]:
        """Paginate through markets. max_rows=0 means unlimited."""
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self.list_markets(limit=page_size, offset=offset, **filters)
            if not page:
                break
            out.extend(page)
            if max_rows and len(out) >= max_rows:
                out = out[:max_rows]
                break
            if len(page) < page_size:
                break
            offset += page_size
        return out

    async def get_market(self, market_id: str | int) -> dict[str, Any] | None:
        """Single market by numeric id, condition id, or slug."""
        return await self.get(f"/markets/{market_id}")

    # ----------------------------------------------------------- events

    async def list_events(
        self,
        *,
        active: bool = True,
        closed: bool | None = None,
        archived: bool | None = None,
        featured: bool | None = None,
        tag_id: int | None = None,
        limit: int = 200,
        offset: int = 0,
        order: str = "volume",
        ascending: bool = False,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": limit, "offset": offset,
            "order": order, "ascending": str(ascending).lower(),
            "active": str(active).lower(),
        }
        if closed is not None:
            params["closed"] = str(closed).lower()
        if archived is not None:
            params["archived"] = str(archived).lower()
        if featured is not None:
            params["featured"] = str(featured).lower()
        if tag_id is not None:
            params["tag_id"] = tag_id
        data = await self.get("/events", **params)
        return data if isinstance(data, list) else data.get("data", [])

    async def iter_events(
        self, *, page_size: int = 200, max_rows: int = 0, **filters: Any
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        offset = 0
        while True:
            page = await self.list_events(limit=page_size, offset=offset, **filters)
            if not page:
                break
            out.extend(page)
            if max_rows and len(out) >= max_rows:
                out = out[:max_rows]
                break
            if len(page) < page_size:
                break
            offset += page_size
        return out

    async def get_event(self, event_id: str | int) -> dict[str, Any] | None:
        return await self.get(f"/events/{event_id}")

    # ------------------------------------------------------------- tags

    async def list_tags(
        self,
        *,
        limit: int = 500,
        offset: int = 0,
        is_carousel: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if is_carousel is not None:
            params["is_carousel"] = str(is_carousel).lower()
        data = await self.get("/tags", **params)
        return data if isinstance(data, list) else data.get("data", [])

    async def get_tag(self, tag_id: str | int) -> dict[str, Any] | None:
        return await self.get(f"/tags/{tag_id}")

    # ----------------------------------------------------------- series

    async def list_series(
        self, *, limit: int = 200, offset: int = 0
    ) -> list[dict[str, Any]]:
        data = await self.get("/series", limit=limit, offset=offset)
        return data if isinstance(data, list) else data.get("data", [])

    async def get_series(self, series_id: str | int) -> dict[str, Any] | None:
        return await self.get(f"/series/{series_id}")

    # ---------------------------------------------------------- comments

    async def list_comments(
        self,
        *,
        parent_entity_type: str | None = None,  # "Market" | "Event"
        parent_entity_id: str | int | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if parent_entity_type:
            params["parent_entity_type"] = parent_entity_type
        if parent_entity_id is not None:
            params["parent_entity_id"] = parent_entity_id
        data = await self.get("/comments", **params)
        return data if isinstance(data, list) else data.get("data", [])
