"""Collectors — long-running async tasks that pull data into the DB."""

from bbl.collectors.base import BaseCollector, CollectorResult
from bbl.collectors.events import EventsCollector
from bbl.collectors.leaderboard import LeaderboardCollector
from bbl.collectors.markets import MarketsCollector
from bbl.collectors.orderbooks import OrderbookCollector
from bbl.collectors.price_history import PriceHistoryCollector
from bbl.collectors.prices import PricesCollector
from bbl.collectors.resolutions import ResolutionsCollector
from bbl.collectors.trades import TradesCollector

ALL_COLLECTORS = {
    "markets": MarketsCollector,
    "events": EventsCollector,
    "prices": PricesCollector,
    "price_history": PriceHistoryCollector,
    "orderbooks": OrderbookCollector,
    "trades": TradesCollector,
    "leaderboard": LeaderboardCollector,
    "resolutions": ResolutionsCollector,
}

__all__ = [
    "ALL_COLLECTORS",
    "BaseCollector",
    "CollectorResult",
    "EventsCollector",
    "LeaderboardCollector",
    "MarketsCollector",
    "OrderbookCollector",
    "PriceHistoryCollector",
    "PricesCollector",
    "ResolutionsCollector",
    "TradesCollector",
]
