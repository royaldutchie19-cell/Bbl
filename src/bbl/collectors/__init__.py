"""Collectors — long-running async tasks that pull data into the DB."""

from bbl.collectors.base import BaseCollector, CollectorResult
from bbl.collectors.leaderboard import LeaderboardCollector
from bbl.collectors.markets import MarketsCollector
from bbl.collectors.orderbooks import OrderbookCollector
from bbl.collectors.prices import PricesCollector
from bbl.collectors.resolutions import ResolutionsCollector
from bbl.collectors.trades import TradesCollector

ALL_COLLECTORS = {
    "markets": MarketsCollector,
    "prices": PricesCollector,
    "orderbooks": OrderbookCollector,
    "trades": TradesCollector,
    "leaderboard": LeaderboardCollector,
    "resolutions": ResolutionsCollector,
}

__all__ = [
    "ALL_COLLECTORS",
    "BaseCollector",
    "CollectorResult",
    "LeaderboardCollector",
    "MarketsCollector",
    "OrderbookCollector",
    "PricesCollector",
    "ResolutionsCollector",
    "TradesCollector",
]
