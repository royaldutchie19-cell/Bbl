"""Smart money convergence signals.

Detects when multiple profitable wallets enter the same market within a
short time window. If 5+ smart wallets buy the same token in 6 hours,
that's a strong signal (Bullpen calls this "Trending"). 2-4 is "Heating Up".
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict

from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)

WINDOW_SECS = 6 * 3600
MIN_WALLETS_HEATING = 2
MIN_WALLETS_TRENDING = 5


def detect_smart_money_signals(cfg: Config, db: Database) -> int:
    """Scan recent trades for smart money convergence. Returns signals written."""
    now = int(time.time())
    lookback = now - 24 * 3600

    smart_addrs = _get_smart_wallets(db, top_n=200)
    if len(smart_addrs) < MIN_WALLETS_HEATING:
        log.info("not enough profitable wallets (%d) to detect signals", len(smart_addrs))
        return 0

    smart_set = set(smart_addrs)

    trades = db.conn.execute(
        """
        SELECT ts, token_id, condition_id, taker, side, price, usdc_size
          FROM trades
         WHERE ts >= ? AND taker IS NOT NULL
         ORDER BY ts
        """,
        (lookback,),
    ).fetchall()

    by_token: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        if t["taker"] in smart_set:
            by_token[t["token_id"]].append({
                "ts": t["ts"],
                "condition_id": t["condition_id"],
                "address": t["taker"],
                "side": t["side"],
                "price": float(t["price"] or 0),
                "usdc": float(t["usdc_size"] or 0),
            })

    signals_written = 0
    with db.tx() as cur:
        for token_id, smart_trades in by_token.items():
            signals = _find_clusters(token_id, smart_trades)
            for sig in signals:
                cur.execute(
                    """
                    INSERT INTO smart_money_signals
                        (ts, condition_id, token_id, direction,
                         signal_strength, trader_count, total_usdc,
                         avg_entry_price, traders_json, note)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        sig["ts"],
                        sig["condition_id"],
                        token_id,
                        sig["direction"],
                        sig["strength"],
                        sig["count"],
                        sig["total_usdc"],
                        sig["avg_price"],
                        json.dumps(sig["traders"]),
                        sig["note"],
                    ),
                )
                signals_written += 1

    log.info("detected %d smart money signals", signals_written)
    return signals_written


def _get_smart_wallets(db: Database, top_n: int = 200) -> list[str]:
    return [
        r["address"]
        for r in db.conn.execute(
            """
            SELECT address FROM traders
             WHERE COALESCE(total_pnl_usdc, 0) > 0
             ORDER BY total_pnl_usdc DESC
             LIMIT ?
            """,
            (top_n,),
        )
    ]


def _find_clusters(token_id: str, trades: list[dict]) -> list[dict]:
    if not trades:
        return []

    trades.sort(key=lambda t: t["ts"])
    signals = []
    seen_windows: set[tuple[str, int]] = set()

    for side in ("BUY", "SELL"):
        side_trades = [t for t in trades if t["side"] == side]
        if len(side_trades) < MIN_WALLETS_HEATING:
            continue

        for i, anchor in enumerate(side_trades):
            window_end = anchor["ts"] + WINDOW_SECS
            cluster = []
            seen_addrs: set[str] = set()

            for t in side_trades[i:]:
                if t["ts"] > window_end:
                    break
                if t["address"] not in seen_addrs:
                    seen_addrs.add(t["address"])
                    cluster.append(t)

            if len(cluster) < MIN_WALLETS_HEATING:
                continue

            sig_key = (side, anchor["ts"] // WINDOW_SECS)
            if sig_key in seen_windows:
                continue
            seen_windows.add(sig_key)

            total_usdc = sum(t["usdc"] for t in cluster)
            prices = [t["price"] for t in cluster if t["price"] > 0]
            avg_price = sum(prices) / len(prices) if prices else 0

            count = len(cluster)
            if count >= MIN_WALLETS_TRENDING:
                strength = min(1.0, 0.6 + (count - MIN_WALLETS_TRENDING) * 0.08)
                note = "trending"
            else:
                strength = 0.2 + (count - MIN_WALLETS_HEATING) * 0.1
                note = "heating_up"

            direction = "bullish" if side == "BUY" else "bearish"

            signals.append({
                "ts": anchor["ts"],
                "condition_id": cluster[0]["condition_id"],
                "direction": direction,
                "strength": round(strength, 3),
                "count": count,
                "total_usdc": round(total_usdc, 2),
                "avg_price": round(avg_price, 4),
                "traders": [
                    {"address": t["address"], "usdc": round(t["usdc"], 2), "price": round(t["price"], 4)}
                    for t in cluster
                ],
                "note": note,
            })

    return signals
