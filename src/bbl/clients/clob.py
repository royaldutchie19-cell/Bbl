"""CLOB API — orderbooks, prices, price history, rewards, on-chain config.

Docs: https://docs.polymarket.com/developers/CLOB/introduction

Endpoints covered (public, no auth):
  /markets                       GET     list markets (cursor paged)
  /markets/{condition_id}        GET     single market
  /simplified-markets            GET     lightweight list
  /sampling-markets              GET     reward-sampling markets
  /book, /books                  GET/POST orderbook(s)
  /price, /midpoint, /midpoints  GET/POST price + midpoint
  /spread, /spreads              GET/POST
  /last-trade-price              GET     last print
  /last-trades-prices            POST    batch last prints
  /prices-history                GET     OHLC candles for a token
  /trades                        GET     CLOB fills
  /rewards/markets               GET     markets eligible for rewards
  /rewards/user/{addr}           GET     per-user rewards
  /rewards/earnings              GET     earnings breakdown
"""

from __future__ import annotations

from typing import Any, Literal

from bbl.clients.base import BaseClient
from bbl.config import APIConfig

Interval = Literal["1m", "1h", "6h", "1d", "1w", "max"]


class ClobClient(BaseClient):
    def __init__(self, cfg: APIConfig):
        super().__init__(cfg, base_url=cfg.clob_base)

    # ----------------------------------------------------------- markets

    async def list_markets(
        self, *, next_cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self.get("/markets", **params)

    async def iter_markets(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            page = await self.list_markets(next_cursor=cursor)
            out.extend(page.get("data", []))
            cursor = page.get("next_cursor")
            if not cursor or cursor == "LTE=":
                break
        return out

    async def get_market(self, condition_id: str) -> dict[str, Any] | None:
        return await self.get(f"/markets/{condition_id}")

    async def list_simplified_markets(
        self, *, next_cursor: str | None = None
    ) -> dict[str, Any]:
        """Same as /markets but returns a stripped-down shape — cheaper to page."""
        params: dict[str, Any] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self.get("/simplified-markets", **params)

    async def list_sampling_markets(
        self, *, next_cursor: str | None = None
    ) -> dict[str, Any]:
        """Markets currently accruing liquidity rewards."""
        params: dict[str, Any] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self.get("/sampling-markets", **params)

    # ------------------------------------------------------- orderbook

    async def get_orderbook(self, token_id: str) -> dict[str, Any]:
        return await self.get("/book", token_id=token_id)

    async def get_orderbooks(self, token_ids: list[str]) -> list[dict[str, Any]]:
        payload = [{"token_id": t} for t in token_ids]
        data = await self.post("/books", json=payload)
        return data if isinstance(data, list) else []

    # ----------------------------------------------------------- prices

    async def get_price(self, token_id: str, side: str = "buy") -> dict[str, Any]:
        return await self.get("/price", token_id=token_id, side=side)

    async def get_midpoint(self, token_id: str) -> dict[str, Any]:
        return await self.get("/midpoint", token_id=token_id)

    async def get_midpoints(self, token_ids: list[str]) -> dict[str, Any]:
        payload = [{"token_id": t} for t in token_ids]
        return await self.post("/midpoints", json=payload)

    async def get_last_trade_price(self, token_id: str) -> dict[str, Any]:
        return await self.get("/last-trade-price", token_id=token_id)

    async def get_last_trades_prices(self, token_ids: list[str]) -> list[dict[str, Any]]:
        payload = [{"token_id": t} for t in token_ids]
        data = await self.post("/last-trades-prices", json=payload)
        return data if isinstance(data, list) else []

    async def get_spread(self, token_id: str) -> dict[str, Any]:
        return await self.get("/spread", token_id=token_id)

    async def get_spreads(self, token_ids: list[str]) -> dict[str, Any]:
        payload = [{"token_id": t} for t in token_ids]
        return await self.post("/spreads", json=payload)

    async def get_price_history(
        self,
        *,
        token_id: str,
        interval: Interval | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
        fidelity: int | None = None,
    ) -> list[dict[str, Any]]:
        """OHLC candles for a token.

        Either `interval` (symbolic range) or `start_ts`+`end_ts` (absolute).
        `fidelity` = minutes per candle (e.g. 1, 5, 60). Response fields:
            t (unix seconds), p (price), plus sometimes h/l/o/c/v depending
            on server version.
        """
        params: dict[str, Any] = {"market": token_id}
        if interval:
            params["interval"] = interval
        if start_ts is not None:
            params["startTs"] = start_ts
        if end_ts is not None:
            params["endTs"] = end_ts
        if fidelity is not None:
            params["fidelity"] = fidelity
        data = await self.get("/prices-history", **params)
        return data.get("history", []) if isinstance(data, dict) else (data or [])

    # ------------------------------------------------------------ trades

    async def get_trades(
        self,
        *,
        market: str | None = None,
        maker: str | None = None,
        taker: str | None = None,
        limit: int = 500,
        next_cursor: str | None = None,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        """CLOB-side trades. Separate from data-api `/trades` — this one
        reflects on-chain fills from the exchange contract directly."""
        params: dict[str, Any] = {"limit": limit}
        if market:
            params["market"] = market
        if maker:
            params["maker"] = maker
        if taker:
            params["taker"] = taker
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self.get("/trades", **params)

    # ---------------------------------------------------------- rewards

    async def get_rewards_markets(
        self, *, next_cursor: str | None = None
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        return await self.get("/rewards/markets", **params)

    async def get_user_rewards(self, address: str) -> dict[str, Any] | None:
        return await self.get(f"/rewards/user/{address}")

    async def get_rewards_earnings(
        self,
        *,
        address: str | None = None,
        date: str | None = None,
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {}
        if address:
            params["user_address"] = address
        if date:
            params["date"] = date
        return await self.get("/rewards/earnings", **params)
