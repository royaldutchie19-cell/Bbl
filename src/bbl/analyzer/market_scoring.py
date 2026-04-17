"""Market scoring — composite score per market for prioritizing attention.

Dimensions (inspired by Bullpen's ScoringFunction):
  - volume_24h: raw 24h USDC volume
  - volume_velocity: 24h vol / 7d avg daily vol (>1 = accelerating)
  - smart_money_flow: net USDC from profitable wallets (pos = bullish)
  - trader_influx: unique new wallets trading in last 24h
  - spread_quality: 0..1 based on latest spread (tighter = higher)
  - holder_concentration: gini coefficient of top holders
  - composite_score: weighted blend of normalized dimensions
"""

from __future__ import annotations

import json
import logging
import math
import time

from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)

WEIGHTS = {
    "volume_velocity": 0.20,
    "smart_money_flow": 0.25,
    "trader_influx": 0.15,
    "spread_quality": 0.15,
    "volume_24h_norm": 0.15,
    "holder_concentration": 0.10,
}


def score_markets(cfg: Config, db: Database) -> int:
    """Compute composite score for every active market. Returns rows written."""
    now = int(time.time())
    ts_24h = now - 86400
    ts_7d = now - 7 * 86400

    markets = db.conn.execute(
        "SELECT condition_id FROM markets WHERE active=1 AND closed=0"
    ).fetchall()
    if not markets:
        return 0

    smart_set = set(
        r["address"]
        for r in db.conn.execute(
            "SELECT address FROM traders WHERE COALESCE(total_pnl_usdc, 0) > 0 "
            "ORDER BY total_pnl_usdc DESC LIMIT 200"
        )
    )

    raw: list[dict] = []
    for m in markets:
        cid = m["condition_id"]
        dims = _compute_dimensions(db, cid, smart_set, now, ts_24h, ts_7d)
        dims["condition_id"] = cid
        raw.append(dims)

    if not raw:
        return 0

    _normalize_and_score(raw)

    written = 0
    with db.tx() as cur:
        for d in raw:
            cur.execute(
                """
                INSERT OR REPLACE INTO market_scores
                    (condition_id, computed_ts, volume_24h, volume_velocity,
                     smart_money_flow, trader_influx, spread_quality,
                     holder_concentration, composite_score, breakdown_json)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    d["condition_id"],
                    now,
                    d.get("volume_24h", 0),
                    d.get("volume_velocity", 0),
                    d.get("smart_money_flow", 0),
                    d.get("trader_influx", 0),
                    d.get("spread_quality", 0),
                    d.get("holder_concentration", 0),
                    d.get("composite_score", 0),
                    json.dumps({k: round(v, 4) for k, v in d.items()
                                if k not in ("condition_id", "composite_score")}),
                ),
            )
            written += 1

    log.info("scored %d markets", written)
    return written


def _compute_dimensions(
    db: Database, cid: str, smart_set: set[str],
    now: int, ts_24h: int, ts_7d: int,
) -> dict:
    d: dict = {}

    vol_24h_row = db.conn.execute(
        "SELECT COALESCE(SUM(usdc_size), 0) AS v FROM trades "
        "WHERE condition_id = ? AND ts >= ?",
        (cid, ts_24h),
    ).fetchone()
    d["volume_24h"] = float(vol_24h_row["v"])

    vol_7d_row = db.conn.execute(
        "SELECT COALESCE(SUM(usdc_size), 0) AS v FROM trades "
        "WHERE condition_id = ? AND ts >= ?",
        (cid, ts_7d),
    ).fetchone()
    avg_daily_7d = float(vol_7d_row["v"]) / 7.0
    d["volume_velocity"] = d["volume_24h"] / avg_daily_7d if avg_daily_7d > 0 else 0.0

    smart_flow = db.conn.execute(
        """
        SELECT COALESCE(SUM(CASE WHEN side='BUY' THEN usdc_size ELSE -usdc_size END), 0) AS net
          FROM trades
         WHERE condition_id = ? AND ts >= ? AND taker IN
               (SELECT address FROM traders WHERE COALESCE(total_pnl_usdc,0) > 0
                ORDER BY total_pnl_usdc DESC LIMIT 200)
        """,
        (cid, ts_24h),
    ).fetchone()
    d["smart_money_flow"] = float(smart_flow["net"])

    influx_row = db.conn.execute(
        "SELECT COUNT(DISTINCT taker) AS n FROM trades "
        "WHERE condition_id = ? AND ts >= ?",
        (cid, ts_24h),
    ).fetchone()
    d["trader_influx"] = float(influx_row["n"])

    spread_row = db.conn.execute(
        """
        SELECT AVG(spread) AS avg_spread FROM price_snapshots ps
        JOIN market_tokens mt ON mt.token_id = ps.token_id
        WHERE mt.condition_id = ? AND ps.ts >= ?
        """,
        (cid, ts_24h),
    ).fetchone()
    avg_spread = float(spread_row["avg_spread"] or 0.1)
    d["spread_quality"] = max(0.0, min(1.0, 1.0 - avg_spread * 10))

    holders = db.conn.execute(
        """
        SELECT shares FROM market_holders
        WHERE condition_id = ?
          AND ts = (SELECT MAX(ts) FROM market_holders WHERE condition_id = ?)
        ORDER BY shares DESC
        """,
        (cid, cid),
    ).fetchall()
    d["holder_concentration"] = _gini([float(h["shares"]) for h in holders]) if holders else 0.0

    return d


def _normalize_and_score(raw: list[dict]) -> None:
    keys = ["volume_24h", "volume_velocity", "smart_money_flow",
            "trader_influx", "spread_quality", "holder_concentration"]

    for key in keys:
        values = [d.get(key, 0) for d in raw]
        lo, hi = min(values), max(values)
        span = hi - lo if hi > lo else 1.0
        norm_key = key + "_norm" if key != "spread_quality" else key
        for d in raw:
            d[norm_key] = (d.get(key, 0) - lo) / span

    for d in raw:
        score = 0.0
        for dim, weight in WEIGHTS.items():
            score += d.get(dim, 0) * weight
        d["composite_score"] = round(min(1.0, max(0.0, score)), 4)


def _gini(values: list[float]) -> float:
    if not values or len(values) < 2:
        return 0.0
    values = sorted(values)
    n = len(values)
    total = sum(values)
    if total == 0:
        return 0.0
    cum = sum((2 * i - n + 1) * v for i, v in enumerate(values))
    return cum / (n * total)
