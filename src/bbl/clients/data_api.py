"""Data API — trades, positions, activity, leaderboard.

The data-api surface is what the Polymarket frontend uses for "Activity",
"Positions", and the leaderboards. It's undocumented-but-stable: endpoints
are observable from the web app's network traffic.
"""

from __future__ import annotations

from typing import Any

from bbl.clients.base import BaseClient
from bbl.config import APIConfig


class DataClient(BaseClient):
    def __init__(self, cfg: APIConfig):
        super().__init__(cfg, base_url=cfg.data_base)

    # --- trades / activity -------------------------------------------------

    async def get_trades(
        self,
        *,
        market: str | None = None,
        user: str | None = None,
        limit: int = 500,
        offset: int = 0,
        taker_only: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if market:
            params["market"] = market
        if user:
            params["user"] = user
        if taker_only is not None:
            params["takerOnly"] = str(taker_only).lower()
        data = await self.get("/trades", **params)
        return data if isinstance(data, list) else data.get("data", [])

    async def get_activity(
        self,
        *,
        user: str,
        limit: int = 500,
        offset: int = 0,
        market: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user": user, "limit": limit, "offset": offset}
        if market:
            params["market"] = market
        data = await self.get("/activity", **params)
        return data if isinstance(data, list) else data.get("data", [])

    # --- positions --------------------------------------------------------

    async def get_positions(
        self,
        *,
        user: str,
        size_threshold: float = 1.0,
        limit: int = 500,
        offset: int = 0,
        redeemable: bool | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "user": user,
            "sizeThreshold": size_threshold,
            "limit": limit,
            "offset": offset,
        }
        if redeemable is not None:
            params["redeemable"] = str(redeemable).lower()
        data = await self.get("/positions", **params)
        return data if isinstance(data, list) else data.get("data", [])

    # --- leaderboard / stats ---------------------------------------------

    async def get_leaderboard(
        self,
        *,
        window: str = "all",  # "day" | "week" | "month" | "all"
        metric: str = "profit",  # "profit" | "volume"
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        data = await self.get(
            "/leaderboard", window=window, metric=metric, limit=limit
        )
        return data if isinstance(data, list) else data.get("data", [])

    async def get_user(self, user: str) -> dict[str, Any] | None:
        """Profile stats (total volume, PnL, trade count, etc)."""
        return await self.get(f"/user/{user}")

    async def get_value(self, user: str) -> dict[str, Any] | None:
        """Current portfolio value for a wallet."""
        return await self.get("/value", user=user)

    async def get_holders(self, market: str, limit: int = 100) -> list[dict[str, Any]]:
        data = await self.get("/holders", market=market, limit=limit)
        return data if isinstance(data, list) else data.get("data", [])
