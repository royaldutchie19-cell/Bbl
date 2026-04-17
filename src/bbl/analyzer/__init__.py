"""Analyzer — derive trader metrics, detect patterns, link wallets."""

from bbl.analyzer.market_scoring import score_markets
from bbl.analyzer.onchain_links import build_funding_links, fetch_funding_for_top_wallets
from bbl.analyzer.patterns import detect_patterns
from bbl.analyzer.performance import compute_trader_metrics
from bbl.analyzer.smart_money import detect_smart_money_signals
from bbl.analyzer.taxonomy import classify_traders
from bbl.analyzer.wallet_linking import find_wallet_links

__all__ = [
    "build_funding_links",
    "classify_traders",
    "compute_trader_metrics",
    "detect_patterns",
    "detect_smart_money_signals",
    "fetch_funding_for_top_wallets",
    "find_wallet_links",
    "score_markets",
]
