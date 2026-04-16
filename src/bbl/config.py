"""Runtime configuration loaded from env vars and optional YAML file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_DATA_DIR = Path(os.getenv("BBL_DATA_DIR", "./data"))
DEFAULT_DB_PATH = DEFAULT_DATA_DIR / "bbl.sqlite"


@dataclass
class APIConfig:
    """Endpoints for Polymarket's three public surfaces."""

    gamma_base: str = "https://gamma-api.polymarket.com"
    clob_base: str = "https://clob.polymarket.com"
    data_base: str = "https://data-api.polymarket.com"
    request_timeout_s: float = 30.0
    # Be a polite citizen — max concurrent in-flight requests per client.
    max_concurrency: int = 8
    # Simple token-bucket rate limits (requests per second).
    rps: float = 5.0
    user_agent: str = "bbl/0.1 (+https://github.com/royaldutchie19-cell/bbl)"


@dataclass
class CollectorConfig:
    """Polling cadence for each collector, in seconds."""

    markets_interval_s: int = 300          # market metadata changes slowly
    prices_interval_s: int = 30            # price snapshots
    orderbook_interval_s: int = 60         # orderbook snapshots for tracked markets
    trades_interval_s: int = 60            # recent trades
    leaderboard_interval_s: int = 900      # leaderboard refresh
    # Cap how many "active" markets we fan out to for orderbook polling,
    # ordered by volume — keep the radar tight.
    top_markets_limit: int = 200


@dataclass
class AnalyzerConfig:
    min_trades_for_ranking: int = 20
    min_volume_usdc: float = 1000.0
    # Wallet linking heuristics
    link_time_window_s: int = 120          # trades within this window are "correlated"
    link_min_overlap: int = 5              # min matching trades to flag a linked wallet
    new_wallet_max_age_days: int = 30


@dataclass
class Config:
    api: APIConfig = field(default_factory=APIConfig)
    collector: CollectorConfig = field(default_factory=CollectorConfig)
    analyzer: AnalyzerConfig = field(default_factory=AnalyzerConfig)
    data_dir: Path = DEFAULT_DATA_DIR
    db_path: Path = DEFAULT_DB_PATH
    log_level: str = os.getenv("BBL_LOG_LEVEL", "INFO")

    @classmethod
    def load(cls, path: str | Path | None = None) -> Config:
        cfg = cls()
        if path is None:
            for candidate in ("config.yaml", "config.yml"):
                if Path(candidate).exists():
                    path = candidate
                    break
        if path and Path(path).exists():
            with open(path) as f:
                raw = yaml.safe_load(f) or {}
            cfg = _merge(cfg, raw)
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        return cfg


def _merge(cfg: Config, raw: dict) -> Config:
    for section in ("api", "collector", "analyzer"):
        if section in raw:
            target = getattr(cfg, section)
            for k, v in raw[section].items():
                if hasattr(target, k):
                    setattr(target, k, v)
    if "data_dir" in raw:
        cfg.data_dir = Path(raw["data_dir"])
        cfg.db_path = cfg.data_dir / "bbl.sqlite"
    if "db_path" in raw:
        cfg.db_path = Path(raw["db_path"])
    if "log_level" in raw:
        cfg.log_level = raw["log_level"]
    return cfg
