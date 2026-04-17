"""Trader taxonomy — classify wallets into archetypes, tiers, and copyability.

Inspired by Bullpen's WalletProfile taxonomy:
  - trader_archetype: whale, sniper, grinder, degen, flipper, diamond_hands
  - risk_profile: conservative, moderate, aggressive
  - trader_tier: newbie, growing, experienced, elite
  - copyability_score: 0..1 (higher = more consistent, more copy-worthy)
"""

from __future__ import annotations

import json
import logging
import time

from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)


def classify_traders(cfg: Config, db: Database) -> int:
    """Assign archetype, tier, risk profile and copyability to each trader.

    Reads from traders + trader_metrics. Writes taxonomy columns into
    trader_metrics.notes as JSON (avoids schema migration).
    """
    rows = db.conn.execute(
        """
        SELECT t.address,
               COALESCE(t.total_pnl_usdc, 0)    AS pnl,
               COALESCE(t.total_volume_usdc, 0)  AS volume,
               COALESCE(t.trade_count, 0)         AS trades,
               tm.win_rate, tm.early_entry_score,
               tm.avg_holding_secs, tm.avg_trade_size,
               tm.markets_traded, tm.contrarian_score
          FROM traders t
          LEFT JOIN trader_metrics tm ON tm.address = t.address
         WHERE COALESCE(t.trade_count, 0) >= 5
         ORDER BY pnl DESC
        """
    ).fetchall()

    if not rows:
        return 0

    pnl_values = sorted([float(r["pnl"]) for r in rows], reverse=True)
    vol_values = sorted([float(r["volume"]) for r in rows], reverse=True)

    updated = 0
    with db.tx() as cur:
        for r in rows:
            pnl = float(r["pnl"])
            volume = float(r["volume"])
            trades = int(r["trades"])
            win_rate = float(r["win_rate"] or 0)
            early = float(r["early_entry_score"] or 1)
            hold_secs = float(r["avg_holding_secs"] or 0)
            avg_size = float(r["avg_trade_size"] or 0)
            markets = int(r["markets_traded"] or 0)
            contrarian = float(r["contrarian_score"] or 0)

            archetype = _archetype(pnl, volume, trades, win_rate, early,
                                   hold_secs, avg_size, markets, contrarian,
                                   pnl_values, vol_values)
            tier = _tier(pnl, volume, trades, pnl_values)
            risk = _risk_profile(avg_size, volume, trades, contrarian, hold_secs)
            copyability = _copyability(pnl, volume, win_rate, trades, markets, early)

            taxonomy = {
                "archetype": archetype,
                "tier": tier,
                "risk_profile": risk,
                "copyability": round(copyability, 3),
            }

            cur.execute(
                "UPDATE trader_metrics SET notes = ? WHERE address = ?",
                (json.dumps(taxonomy), r["address"]),
            )
            updated += 1

    log.info("classified %d traders", updated)
    return updated


def _percentile_rank(value: float, sorted_desc: list[float]) -> float:
    if not sorted_desc:
        return 0.0
    rank = sum(1 for v in sorted_desc if v >= value)
    return 1.0 - (rank / len(sorted_desc))


def _archetype(
    pnl: float, volume: float, trades: int, win_rate: float,
    early: float, hold_secs: float, avg_size: float, markets: int,
    contrarian: float, pnl_sorted: list[float], vol_sorted: list[float],
) -> str:
    pnl_pct = _percentile_rank(pnl, pnl_sorted)
    vol_pct = _percentile_rank(volume, vol_sorted)
    hold_hours = hold_secs / 3600

    if vol_pct > 0.95 and avg_size > 5000:
        return "whale"
    if win_rate > 0.6 and early < 0.2 and trades >= 20:
        return "sniper"
    if hold_hours < 2 and trades > 100:
        return "flipper"
    if contrarian > 0.3 and pnl_pct > 0.7:
        return "contrarian"
    if hold_hours > 72 and trades < 50:
        return "diamond_hands"
    if trades > 200 and markets > 10:
        return "grinder"
    if vol_pct > 0.8 and win_rate < 0.4:
        return "degen"
    return "trader"


def _tier(pnl: float, volume: float, trades: int, pnl_sorted: list[float]) -> str:
    pnl_pct = _percentile_rank(pnl, pnl_sorted)
    if pnl_pct > 0.95 and trades >= 50:
        return "elite"
    if pnl_pct > 0.75 and trades >= 30:
        return "experienced"
    if pnl_pct > 0.4 and trades >= 10:
        return "growing"
    return "newbie"


def _risk_profile(avg_size: float, volume: float, trades: int,
                  contrarian: float, hold_secs: float) -> str:
    avg_vol_per_trade = volume / trades if trades > 0 else 0
    if avg_size > 5000 or contrarian > 0.3:
        return "aggressive"
    if avg_vol_per_trade < 500 and hold_secs > 86400:
        return "conservative"
    return "moderate"


def _copyability(pnl: float, volume: float, win_rate: float,
                 trades: int, markets: int, early: float) -> float:
    if trades < 10 or volume < 1000:
        return 0.0
    roi = pnl / volume if volume > 0 else 0
    score = 0.0
    score += min(0.3, max(0, roi) * 1.5)
    score += min(0.25, win_rate * 0.4)
    score += min(0.15, trades / 500)
    score += min(0.15, markets / 30)
    score += min(0.15, max(0, 1 - early) * 0.2)
    return min(1.0, score)
