"""Analyzer — derive trader metrics, detect patterns, link wallets."""

from bbl.analyzer.onchain_links import build_funding_links, fetch_funding_for_top_wallets
from bbl.analyzer.patterns import detect_patterns
from bbl.analyzer.performance import compute_trader_metrics
from bbl.analyzer.wallet_linking import find_wallet_links

__all__ = [
    "build_funding_links",
    "compute_trader_metrics",
    "detect_patterns",
    "fetch_funding_for_top_wallets",
    "find_wallet_links",
]
