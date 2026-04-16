"""CLOB API — order books, prices, on-chain market config.

The CLOB API exposes per-token orderbooks (bids/asks), midpoint/last prices,
and the canonical list of tradable markets with their ERC1155 token IDs.
Docs: https://docs.polymarket.com/developers/CLOB/introduction
"""

from __future__ import annotations

from typing import Any

from bbl.clients.base import BaseClient
from bbl.config import APIConfig


class ClobClient(BaseClient):
    def __init__(self, cfg: APIConfig):
        super().__init__(cfg, base_url=cfg.clob_base)

    async def list_markets(
        self, *, next_cursor: str | None = None
    ) -> dict[str, Any]:
        """One page of CLOB markets (cursor-paginated)."""
        params: dict[str, Any] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self.get("/markets", **params)

    async def iter_markets(self) -> list[dict[str, Any]]:
        """Walk the cursor until the CLOB returns its end sentinel 'LTE='."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self.list_markets(next_cursor=cursor)
            out.extend(page.get("data", []))
            cursor = page.get("next_cursor")
            if not cursor or cursor == "LTE=":
                break
        return out

    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        return await self.get("/book", token_id=token_id)

    async def get_orderbooks(self, token_ids: list[str]) -> list[dict[str, Any]]:
        """Batch fetch — POST /books accepts up to ~100 token ids at a time."""
        payload = [{"token_id": t} for t in token_ids]
        data = await self.post("/books", json=payload)
        return data if isinstance(data, list) else []

    async def get_price(self, token_id: str, side: str = "buy") -> dict[str, Any]:
        return await self.get("/price", token_id=token_id, side=side)

    async def get_midpoint(self, token_id: str) -> dict[str, Any]:
        return await self.get("/midpoint", token_id=token_id)

    async def get_midpoints(self, token_ids: list[str]) -> dict[str, Any]:
        payload = [{"token_id": t} for t in token_ids]
        return await self.post("/midpoints", json=payload)

    async def get_last_trade_price(self, token_id: str) -> dict[str, Any]:
        return await self.get("/last-trade-price", token_id=token_id)

    async def get_spread(self, token_id: str) -> dict[str, Any]:
        return await self.get("/spread", token_id=token_id)
