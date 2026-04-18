"""Alert detection for significant market events.

Scans for price moves, large trades, volume spikes, and pair-cost
anomalies within a configurable lookback window.
"""

from __future__ import annotations

import logging
import time

from bbl.storage import Database

log = logging.getLogger(__name__)

PRICE_MOVE_THRESHOLD = 0.05
LARGE_TRADE_USDC = 5_000
VOLUME_SPIKE_MULTIPLIER = 2.0
PAIR_COST_THRESHOLD = 0.98


def detect_alerts(db: Database, lookback_hours: int = 1) -> int:
    """Detect market alerts within the lookback window. Returns alerts generated."""
    now = int(time.time())
    cutoff = now - lookback_hours * 3600

    db.conn.executescript("""
        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,
            alert_type  TEXT NOT NULL,
            condition_id TEXT,
            token_id    TEXT,
            severity    TEXT NOT NULL,
            title       TEXT NOT NULL,
            detail      TEXT,
            value       REAL
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_ts   ON alerts(ts DESC);
        CREATE INDEX IF NOT EXISTS idx_alerts_type ON alerts(alert_type, ts);
    """)

    count = 0
    with db.tx() as cur:
        count += _detect_price_moves(db, cur, now, cutoff)
        count += _detect_large_trades(db, cur, now, cutoff)
        count += _detect_volume_spikes(db, cur, now, cutoff)
        count += _detect_pair_cost_alerts(db, cur, now)

    log.info("generated %d alerts", count)
    return count


def _insert_alert(cur, *, ts, alert_type, condition_id, token_id, severity, title, detail, value):
    cur.execute(
        """
        INSERT INTO alerts (ts, alert_type, condition_id, token_id, severity, title, detail, value)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (ts, alert_type, condition_id, token_id, severity, title, detail, value),
    )
    return 1


def _detect_price_moves(db, cur, now, cutoff):
    tokens = db.conn.execute(
        """
        SELECT DISTINCT ps.token_id, mt.condition_id
          FROM price_snapshots ps
          JOIN market_tokens mt ON mt.token_id = ps.token_id
          JOIN markets m ON m.condition_id = mt.condition_id
         WHERE ps.ts >= ? AND m.active = 1 AND m.closed = 0
        """,
        (cutoff,),
    ).fetchall()

    count = 0
    for tok in tokens:
        earliest = db.conn.execute(
            "SELECT mid FROM price_snapshots WHERE token_id = ? AND ts >= ? ORDER BY ts ASC LIMIT 1",
            (tok["token_id"], cutoff),
        ).fetchone()
        latest = db.conn.execute(
            "SELECT mid FROM price_snapshots WHERE token_id = ? AND ts >= ? ORDER BY ts DESC LIMIT 1",
            (tok["token_id"], cutoff),
        ).fetchone()

        if not earliest or not latest:
            continue
        old_mid = earliest["mid"]
        new_mid = latest["mid"]
        if old_mid is None or new_mid is None or old_mid == 0:
            continue

        change = new_mid - old_mid
        if abs(change) < PRICE_MOVE_THRESHOLD:
            continue

        direction = "up" if change > 0 else "down"
        severity = "critical" if abs(change) >= 0.15 else "warning"
        count += _insert_alert(
            cur,
            ts=now,
            alert_type="price_move",
            condition_id=tok["condition_id"],
            token_id=tok["token_id"],
            severity=severity,
            title=f"Price moved {direction} {abs(change):.1%}",
            detail=f"{old_mid:.4f} -> {new_mid:.4f}",
            value=round(change, 6),
        )
    return count


def _detect_large_trades(db, cur, now, cutoff):
    trades = db.conn.execute(
        """
        SELECT t.trade_id, t.ts, t.condition_id, t.token_id, t.side,
               t.price, t.usdc_size, t.taker
          FROM trades t
          JOIN markets m ON m.condition_id = t.condition_id
         WHERE t.ts >= ? AND t.usdc_size > ? AND m.active = 1
        """,
        (cutoff, LARGE_TRADE_USDC),
    ).fetchall()

    count = 0
    for t in trades:
        usdc = float(t["usdc_size"] or 0)
        severity = "critical" if usdc >= 50_000 else ("warning" if usdc >= 10_000 else "info")
        count += _insert_alert(
            cur,
            ts=now,
            alert_type="large_trade",
            condition_id=t["condition_id"],
            token_id=t["token_id"],
            severity=severity,
            title=f"Large {t['side'] or 'TRADE'} ${usdc:,.0f}",
            detail=f"taker={t['taker']} price={t['price']}",
            value=usdc,
        )
    return count


def _detect_volume_spikes(db, cur, now, cutoff):
    recent = db.conn.execute(
        """
        SELECT condition_id, SUM(usdc_size) AS recent_vol
          FROM trades
         WHERE ts >= ?
         GROUP BY condition_id
        """,
        (cutoff,),
    ).fetchall()

    lookback_7d = now - 7 * 24 * 3600
    count = 0
    for r in recent:
        cid = r["condition_id"]
        recent_vol = float(r["recent_vol"] or 0)
        if recent_vol == 0:
            continue

        hist = db.conn.execute(
            """
            SELECT SUM(usdc_size) AS total_vol,
                   (MAX(ts) - MIN(ts)) AS span_secs
              FROM trades
             WHERE condition_id = ? AND ts >= ? AND ts < ?
            """,
            (cid, lookback_7d, cutoff),
        ).fetchone()

        if not hist or not hist["total_vol"] or not hist["span_secs"]:
            continue

        span_hours = max(float(hist["span_secs"]) / 3600, 1)
        avg_hourly = float(hist["total_vol"]) / span_hours
        if avg_hourly == 0:
            continue

        ratio = recent_vol / avg_hourly
        if ratio < VOLUME_SPIKE_MULTIPLIER:
            continue

        severity = "critical" if ratio >= 5.0 else ("warning" if ratio >= 3.0 else "info")
        count += _insert_alert(
            cur,
            ts=now,
            alert_type="volume_spike",
            condition_id=cid,
            token_id=None,
            severity=severity,
            title=f"Volume spike {ratio:.1f}x average",
            detail=f"recent=${recent_vol:,.0f} avg_hourly=${avg_hourly:,.0f}",
            value=round(ratio, 2),
        )
    return count


def _detect_pair_cost_alerts(db, cur, now):
    rows = db.conn.execute(
        """
        SELECT condition_id, yes_price, no_price
          FROM markets
         WHERE active = 1 AND closed = 0
           AND yes_price IS NOT NULL AND no_price IS NOT NULL
        """
    ).fetchall()

    count = 0
    for r in rows:
        pair_cost = float(r["yes_price"]) + float(r["no_price"])
        if pair_cost >= PAIR_COST_THRESHOLD:
            continue

        gap = 1.0 - pair_cost
        severity = "critical" if gap >= 0.05 else "warning"
        count += _insert_alert(
            cur,
            ts=now,
            alert_type="pair_cost_alert",
            condition_id=r["condition_id"],
            token_id=None,
            severity=severity,
            title=f"Pair cost {pair_cost:.4f} (gap {gap:.2%})",
            detail=f"yes={r['yes_price']:.4f} no={r['no_price']:.4f}",
            value=round(gap, 6),
        )
    return count
