"""Pattern detection — features that correlate with top-trader success.

This module looks at:
  - Category specialization (politics / sports / crypto / ...)
  - Bet sizing distribution (concentration vs. spray-and-pray)
  - Time-of-day activity clusters
  - "Contrarian vs. momentum" behavior — do they trade against consensus?
  - Post-news speed — do they trade within minutes of market move?

Writes findings into trader_metrics.notes (JSON-ish string) and updates
the contrarian_score column.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass

from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)


@dataclass
class TraderPatterns:
    address: str
    category_mix: dict[str, float]
    size_concentration: float         # Gini-like: 0=even, 1=all on one trade
    hour_of_day_peak: int             # UTC hour with most fills
    speed_after_move_p50: float | None  # median seconds between a 5%+ mid-price swing and their trade
    contrarian_score: float           # in [-1, 1], positive = bet against consensus
    top_markets: list[str]


def detect_patterns(cfg: Config, db: Database) -> int:
    """Walk trader_metrics and attach pattern annotations."""
    rows = db.conn.execute(
        "SELECT address FROM trader_metrics ORDER BY realized_pnl DESC"
    ).fetchall()
    updated = 0
    for r in rows:
        patt = _analyze_one(db, r["address"])
        if not patt:
            continue
        db.conn.execute(
            """
            UPDATE trader_metrics
               SET contrarian_score = ?, notes = ?
             WHERE address = ?
            """,
            (patt.contrarian_score, _notes_json(patt), patt.address),
        )
        updated += 1
    log.info("pattern annotations: %d traders", updated)
    return updated


def _analyze_one(db: Database, addr: str) -> TraderPatterns | None:
    fills = db.conn.execute(
        """
        SELECT t.ts, t.condition_id, t.token_id, t.side, t.price, t.size, t.usdc_size,
               m.category
          FROM trades t LEFT JOIN markets m ON m.condition_id = t.condition_id
         WHERE t.taker = ?
         ORDER BY t.ts
        """,
        (addr,),
    ).fetchall()
    if not fills:
        return None

    cat_volume: Counter[str] = Counter()
    total_volume = 0.0
    sizes: list[float] = []
    hours: Counter[int] = Counter()
    markets_volume: Counter[str] = Counter()

    for f in fills:
        usdc = float(f["usdc_size"] or 0)
        total_volume += usdc
        sizes.append(usdc)
        if f["category"]:
            cat_volume[f["category"]] += usdc
        hours[_hour(f["ts"])] += 1
        if f["condition_id"]:
            markets_volume[f["condition_id"]] += usdc

    cat_mix = {k: v / total_volume for k, v in cat_volume.items()} if total_volume else {}
    size_conc = _gini(sizes)
    hour_peak = hours.most_common(1)[0][0] if hours else 0
    contrarian = _contrarian_score(db, fills)
    speed_med = _median_reaction_time(db, fills)

    top_markets = [m for m, _ in markets_volume.most_common(5)]

    return TraderPatterns(
        address=addr,
        category_mix=cat_mix,
        size_concentration=size_conc,
        hour_of_day_peak=hour_peak,
        speed_after_move_p50=speed_med,
        contrarian_score=contrarian,
        top_markets=top_markets,
    )


def _hour(ts: int) -> int:
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(ts).hour


def _gini(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    cum = 0.0
    total = 0.0
    for i, x in enumerate(s, start=1):
        cum += i * x
        total += x
    if total == 0:
        return 0.0
    return (2 * cum) / (n * total) - (n + 1) / n


def _contrarian_score(db: Database, fills: list[sqlite3.Row]) -> float:
    """Compare each fill's price to the nearest mid-price snapshot of the token.

    A BUY well above mid = aggressive momentum (positive "trend-follow").
    A BUY well below mid, or a SELL well above mid = contrarian.
    Returns mean(signed_delta) normalized to roughly [-1, 1].
    """
    deltas: list[float] = []
    for f in fills:
        if not f["token_id"]:
            continue
        row = db.conn.execute(
            """
            SELECT mid FROM price_snapshots
             WHERE token_id = ? AND ts <= ?
             ORDER BY ts DESC LIMIT 1
            """,
            (f["token_id"], f["ts"]),
        ).fetchone()
        if not row or row["mid"] is None:
            continue
        mid = float(row["mid"])
        price = float(f["price"] or 0)
        d = price - mid  # in [-1, 1]
        # Contrarian = buying below mid, selling above mid
        if (f["side"] or "").upper() == "BUY":
            deltas.append(-d)  # below-mid buy → positive
        elif (f["side"] or "").upper() == "SELL":
            deltas.append(d)   # above-mid sell → positive
    if not deltas:
        return 0.0
    # Normalize: divide by average absolute delta to keep in rough bounds
    mean_d = sum(deltas) / len(deltas)
    scale = max(0.05, sum(abs(d) for d in deltas) / len(deltas))
    return max(-1.0, min(1.0, mean_d / scale))


def _median_reaction_time(db: Database, fills: list[sqlite3.Row]) -> float | None:
    """For each fill, find the time since the last 5%+ move in the token mid.
    Shorter = reacts to news/swings faster.
    """
    lags: list[float] = []
    for f in fills:
        if not f["token_id"]:
            continue
        rows = db.conn.execute(
            """
            SELECT ts, mid FROM price_snapshots
             WHERE token_id = ? AND ts < ?
             ORDER BY ts DESC LIMIT 50
            """,
            (f["token_id"], f["ts"]),
        ).fetchall()
        if len(rows) < 2:
            continue
        base_mid = rows[0]["mid"]
        if base_mid is None:
            continue
        for r in rows[1:]:
            if r["mid"] is None:
                continue
            if abs(float(r["mid"]) - float(base_mid)) >= 0.05:
                lags.append(float(f["ts"] - r["ts"]))
                break
    if not lags:
        return None
    lags.sort()
    return lags[len(lags) // 2]


def _notes_json(p: TraderPatterns) -> str:
    return json.dumps(
        {
            "category_mix": p.category_mix,
            "size_concentration": p.size_concentration,
            "hour_of_day_peak": p.hour_of_day_peak,
            "speed_after_move_p50_s": p.speed_after_move_p50,
            "top_markets": p.top_markets,
        },
        default=str,
    )
