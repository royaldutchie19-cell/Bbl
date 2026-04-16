"""Data API — trades, positions, activity, leaderboard, pnl, holdings.

The data-api surface is undocumented but stable: endpoints are observed
from the web app's network traffic. Field names are best-effort and may
drift; if you spot a mismatch, patch here.

Endpoints covered (best-effort — marked * are more experimental):
  /trades              GET  recent fills (public)
  /activity            GET  per-user trade/merge/split/redeem activity
  /positions           GET  current positions
  /holders             GET  top holders for a market
  /leaderboard         GET  ranked wallets (window + metric)
  /user/{addr}         GET  profile + aggregate stats
  /value               GET  current portfolio value
  /pnl                 GET  historical PnL time series *
  /portfolio-value     GET  portfolio value time series *
  /volume              GET  traded-volume stats *
  /rewards             GET  reward earnings *
  /earnings            GET  maker earnings *
  /traded-markets      GET  distinct markets a user has traded *
  /username/{name}     GET  resolve username → address *
"""

from __future__ import annotations

from typing import Any, Literal

from bbl.clients.base import BaseClient
from bbl.config import APIConfig

LeaderboardWindow = Literal["day", "week", "month", "all"]
LeaderboardMetric = Literal["profit", "volume"]


class DataClient(BaseClient):
    def __init__(self, cfg: APIConfig):
        super().__init__(cfg, base_url=cfg.data_base)

    # ---------------------------------------------------- trades / activity

    async def get_trades(
        self,
        *,
        market: str | None = None,
        user: str | None = None,
        limit: int = 500,
        offset: int = 0,
        taker_only: bool | None = None,
        filter_type: str | None = None,  # e.g. "TRADE"
        min_size: float | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": limit, "offset": offset}
        if market:
            params["market"] = market
        if user:
            params["user"] = user
        if taker_only is not None:
            params["takerOnly"] = str(taker_only).lower()
        if filter_type:
            params["filterType"] = filter_type
        if min_size is not None:
            params["filterAmount"] = min_size
        data = await self.get("/trades", **params)
        return data if isinstance(data, list) else data.get("data", [])

    async def get_activity(
        self,
        *,
        user: str,
        limit: int = 500,
        offset: int = 0,
        market: str | None = None,
        types: str | None = None,  # "TRADE,SPLIT,MERGE,REDEEM,REWARD,CONVERSION"
        start: int | None = None,
        end: int | None = None,
        side: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"user": user, "limit": limit, "offset": offset}
        if market:
            params["market"] = market
        if types:
            params["type"] = types
        if start is not None:
            params["start"] = start
        if end is not None:
            params["end"] = end
        if side:
            params["side"] = side
        data = await self.get("/activity", **params)
        return data if isinstance(data, list) else data.get("data", [])

    # -------------------------------------------------------- positions

    async def get_positions(
        self,
        *,
        user: str,
        size_threshold: float = 1.0,
        limit: int = 500,
        offset: int = 0,
        redeemable: bool | None = None,
        market: str | None = None,
        event_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "user": user,
            "sizeThreshold": size_threshold,
            "limit": limit,
            "offset": offset,
        }
        if redeemable is not None:
            params["redeemable"] = str(redeemable).lower()
        if market:
            params["market"] = market
        if event_id:
            params["eventId"] = event_id
        data = await self.get("/positions", **params)
        return data if isinstance(data, list) else data.get("data", [])

    async def get_holders(
        self, market: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        data = await self.get("/holders", market=market, limit=limit)
        return data if isinstance(data, list) else data.get("data", [])

    # ------------------------------------------------------ leaderboards

    async def get_leaderboard(
        self,
        *,
        window: LeaderboardWindow = "all",
        metric: LeaderboardMetric = "profit",
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        data = await self.get(
            "/leaderboard", window=window, metric=metric, limit=limit
        )
        return data if isinstance(data, list) else data.get("data", [])

    # -------------------------------------------------------- user stats

    async def get_user(self, user: str) -> dict[str, Any] | None:
        return await self.get(f"/user/{user}")

    async def resolve_username(self, username: str) -> dict[str, Any] | None:
        """Resolve a display/username to an address+profile."""
        return await self.get(f"/username/{username}")

    async def get_value(self, user: str) -> dict[str, Any] | None:
        return await self.get("/value", user=user)

    async def get_pnl(
        self,
        *,
        user: str,
        interval: str | None = None,  # "1d" | "1w" | "1m" | "all"
        fidelity: int | None = None,  # minutes per point
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        params: dict[str, Any] = {"user": user}
        if interval:
            params["interval"] = interval
        if fidelity is not None:
            params["fidelity"] = fidelity
        return await self.get("/pnl", **params)

    async def get_portfolio_value(
        self,
        *,
        user: str,
        interval: str | None = None,
        fidelity: int | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any] | None:
        params: dict[str, Any] = {"user": user}
        if interval:
            params["interval"] = interval
        if fidelity is not None:
            params["fidelity"] = fidelity
        # Polymarket has used both paths historically — try plural first.
        try:
            return await self.get("/portfolio-value", **params)
        except Exception:
            return await self.get("/portfolio/value", **params)

    async def get_volume(
        self,
        *,
        user: str,
        window: str | None = None,  # "day"|"week"|"month"|"all"
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {"user": user}
        if window:
            params["window"] = window
        return await self.get("/volume", **params)

    async def get_traded_markets(
        self,
        *,
        user: str,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        data = await self.get(
            "/traded-markets", user=user, limit=limit, offset=offset
        )
        return data if isinstance(data, list) else data.get("data", [])

    # ------------------------------------------------------------ rewards

    async def get_user_rewards(
        self, *, user: str
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        return await self.get("/rewards", user=user)

    async def get_user_earnings(
        self, *, user: str, window: str | None = None
    ) -> dict[str, Any] | list[dict[str, Any]] | None:
        params: dict[str, Any] = {"user": user}
        if window:
            params["window"] = window
        return await self.get("/earnings", **params)
