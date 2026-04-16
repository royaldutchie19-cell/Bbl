"""Wallet linking — detect "this new wallet is probably a known top trader".

Top Polymarket traders regularly rotate wallets (fresh identity, reset
leaderboard presence, avoid tax/KYC scrutiny). Detecting the new wallet
early is the whole alpha — we want to copy-trade them *before* they show
up on the public leaderboard.

Signals we combine (each produces a 0..1 sub-score):

  1. **Timing correlation** — same-side, same-token, within N seconds of
     a known top wallet's fill. Easy to forge intentionally, but bulk
     overlap across many markets is high-signal.

  2. **Funding graph** — not possible from data-api alone; leaves a TODO
     hook to enrich from on-chain RPC later.

  3. **Behavioral fingerprint** — bet sizing distribution, category mix,
     hour-of-day activity pattern. Compared against established wallets.

  4. **New-wallet prior** — recently-first-seen wallets get flagged for
     faster copy-trade consideration.

Results are written to `wallet_links` with a combined score and a
comma-separated `reason` tag string.
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
from collections import defaultdict

from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)


def find_wallet_links(cfg: Config, db: Database) -> int:
    """Compute pairwise link scores between known-top wallets and all others.

    Returns number of edges written.
    """
    window = cfg.analyzer.link_time_window_s
    min_overlap = cfg.analyzer.link_min_overlap
    max_age = cfg.analyzer.new_wallet_max_age_days * 86400
    now = int(time.time())

    top_addrs = [
        r["address"]
        for r in db.conn.execute(
            """
            SELECT address FROM trader_metrics
             WHERE realized_pnl > 0
             ORDER BY realized_pnl DESC
             LIMIT 500
            """
        )
    ]
    if not top_addrs:
        log.info("no established trader_metrics yet — run analyze first")
        return 0
    top_set = set(top_addrs)

    # Precompute: per (token_id, side) -> list of (ts, address) for every fill.
    idx: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    fingerprints: dict[str, dict] = {}
    first_seen: dict[str, int] = {}
    for row in db.conn.execute(
        """
        SELECT taker, ts, token_id, side, usdc_size
          FROM trades
         WHERE taker IS NOT NULL AND token_id IS NOT NULL
        """
    ):
        key = (row["token_id"], (row["side"] or "").upper())
        idx[key].append((row["ts"], row["taker"]))
        _update_fingerprint(fingerprints, row)
        first_seen.setdefault(row["taker"], row["ts"])
        if row["ts"] < first_seen[row["taker"]]:
            first_seen[row["taker"]] = row["ts"]

    for key, entries in idx.items():
        entries.sort()

    # For each top wallet, look at its fills and find near-in-time fills by
    # other wallets on the same token + side. Count overlaps per counterparty.
    overlap_counts: dict[tuple[str, str], int] = defaultdict(int)
    for top_addr in top_addrs:
        for row in db.conn.execute(
            "SELECT ts, token_id, side FROM trades WHERE taker = ? AND token_id IS NOT NULL",
            (top_addr,),
        ):
            key = (row["token_id"], (row["side"] or "").upper())
            bucket = idx.get(key)
            if not bucket:
                continue
            ts = row["ts"]
            lo = _bisect(bucket, ts - window)
            hi = _bisect(bucket, ts + window + 1)
            for _, other in bucket[lo:hi]:
                if other == top_addr:
                    continue
                overlap_counts[(top_addr, other)] += 1

    rows_written = 0
    for (a, b), count in overlap_counts.items():
        if count < min_overlap:
            continue
        timing = min(1.0, count / 50.0)
        fp = _fingerprint_similarity(fingerprints.get(a), fingerprints.get(b))
        new_prior = 0.0
        b_age = now - first_seen.get(b, now)
        if b_age < max_age and b not in top_set:
            # fresh wallet shadowing an established top wallet — this is what
            # we're most interested in.
            new_prior = 1.0 - (b_age / max_age)

        score = 0.55 * timing + 0.35 * fp + 0.10 * new_prior
        reasons = []
        if timing > 0.2:
            reasons.append("timing")
        if fp > 0.5:
            reasons.append("fingerprint")
        if new_prior > 0:
            reasons.append("new_wallet")

        evidence = {
            "overlap_trades": count,
            "timing_score": round(timing, 4),
            "fingerprint_score": round(fp, 4),
            "new_wallet_score": round(new_prior, 4),
            "b_first_seen_ts": first_seen.get(b),
        }
        db.conn.execute(
            """
            INSERT INTO wallet_links (address_a, address_b, score, reason,
                evidence_json, first_seen_ts, last_seen_ts)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(address_a, address_b) DO UPDATE SET
                score=excluded.score,
                reason=excluded.reason,
                evidence_json=excluded.evidence_json,
                last_seen_ts=excluded.last_seen_ts
            """,
            (a, b, score, ",".join(reasons), json.dumps(evidence), now, now),
        )
        rows_written += 1
    log.info("wallet_links rows written: %d", rows_written)
    return rows_written


def _bisect(bucket: list[tuple[int, str]], ts: int) -> int:
    lo, hi = 0, len(bucket)
    while lo < hi:
        mid = (lo + hi) // 2
        if bucket[mid][0] < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def _update_fingerprint(fps: dict[str, dict], row: sqlite3.Row) -> None:
    fp = fps.setdefault(
        row["taker"],
        {
            "hours": [0] * 24,
            "size_log_sum": 0.0,
            "size_log_sq": 0.0,
            "n": 0,
        },
    )
    import datetime as _dt

    fp["hours"][_dt.datetime.utcfromtimestamp(row["ts"]).hour] += 1
    size = float(row["usdc_size"] or 0)
    if size > 0:
        l = math.log(size + 1)
        fp["size_log_sum"] += l
        fp["size_log_sq"] += l * l
        fp["n"] += 1


def _fingerprint_similarity(a: dict | None, b: dict | None) -> float:
    if not a or not b or a["n"] == 0 or b["n"] == 0:
        return 0.0
    ha = _normalize(a["hours"])
    hb = _normalize(b["hours"])
    hour_sim = 1.0 - 0.5 * sum(abs(x - y) for x, y in zip(ha, hb))

    mean_a = a["size_log_sum"] / a["n"]
    mean_b = b["size_log_sum"] / b["n"]
    var_a = max(0.0, a["size_log_sq"] / a["n"] - mean_a**2)
    var_b = max(0.0, b["size_log_sq"] / b["n"] - mean_b**2)
    # Similarity: 1 when distributions match, falls off with mean distance
    size_sim = math.exp(-abs(mean_a - mean_b) / 2.0) * math.exp(-abs(var_a - var_b) / 2.0)

    return max(0.0, min(1.0, 0.6 * hour_sim + 0.4 * size_sim))


def _normalize(xs: list[int]) -> list[float]:
    s = sum(xs) or 1
    return [x / s for x in xs]
