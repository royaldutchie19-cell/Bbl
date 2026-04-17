"""Funding-graph based wallet linking.

Pipeline:
  1. For each tracked proxy wallet (top-traders + watchlist), fetch USDC
     Transfer logs into that proxy from Polygon RPC.
  2. Persist transfers and a derived per-(proxy, funder) summary.
  3. Build links: any two proxy wallets sharing a funder become a candidate
     pair. Score scales with transfer count, USDC volume, and funder
     "uniqueness" (a funder that funds 1000 wallets = exchange hot wallet,
     low signal; a funder that funds 2 wallets = high signal).

Writes results into the same `wallet_links` table the trade-timing
analyzer uses, with reason="funding" merged in.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import defaultdict

from bbl.clients.onchain import OnchainClient
from bbl.config import Config
from bbl.storage import Database

log = logging.getLogger(__name__)

USDC_DECIMALS = 6


async def fetch_funding_for_top_wallets(
    cfg: Config,
    db: Database,
    *,
    limit: int = 200,
) -> int:
    """Pull USDC inbound transfers for the top-N traders + watchlist.

    Returns the number of transfer rows inserted.
    """
    addrs = _wallets_to_scan(db, limit=limit)
    if not addrs:
        log.info("no wallets to scan — populate trader_metrics or watchlist first")
        return 0

    inserted = 0
    async with OnchainClient(cfg.onchain) as rpc:
        head = await rpc.block_number()
        for addr in addrs:
            anchor = _last_funding_block(db, addr)
            from_block = (
                anchor + 1
                if anchor is not None
                else max(0, head - cfg.onchain.initial_lookback_blocks)
            )
            if from_block > head:
                continue
            try:
                logs = await rpc.fetch_inbound_usdc(
                    addr, from_block=from_block, to_block=head
                )
            except Exception as e:
                log.warning("funding fetch failed for %s: %s", addr, e)
                continue

            with db.tx() as cur:
                for entry in logs:
                    cur.execute(
                        """
                        INSERT OR IGNORE INTO funding_transfers
                            (proxy_wallet, funder, block_number, tx_hash,
                             log_index, amount_usdc, ts)
                        VALUES (?,?,?,?,?,?,?)
                        """,
                        (
                            addr,
                            entry["from"],
                            entry["block"],
                            entry["tx_hash"],
                            entry["log_index"],
                            entry["value_raw"] / 10**USDC_DECIMALS,
                            None,  # ts: filled later by a separate job to save RPC calls
                        ),
                    )
                    inserted += cur.rowcount
            log.info("funding %s: %d transfers (blocks %d..%d)",
                     addr, len(logs), from_block, head)
    _refresh_wallet_funders(db)
    return inserted


def build_funding_links(cfg: Config, db: Database) -> int:
    """Promote shared-funder relationships into wallet_links rows."""
    # funders that look like exchange hot wallets — funder common to many
    # proxies — are noise. We compute a per-funder "uniqueness" weight.
    funder_proxy_count: dict[str, int] = {}
    for r in db.conn.execute(
        "SELECT funder, COUNT(DISTINCT proxy_wallet) AS c FROM wallet_funders GROUP BY funder"
    ):
        funder_proxy_count[r["funder"]] = r["c"]

    # Group proxies by shared funder
    groups: dict[str, list[tuple[str, int, float]]] = defaultdict(list)
    for r in db.conn.execute(
        "SELECT funder, proxy_wallet, transfer_cnt, total_usdc FROM wallet_funders"
    ):
        groups[r["funder"]].append(
            (r["proxy_wallet"], int(r["transfer_cnt"] or 0), float(r["total_usdc"] or 0))
        )

    # For each pair of proxies sharing a funder, accumulate a score
    pair_score: dict[tuple[str, str], dict] = {}
    for funder, proxies in groups.items():
        if len(proxies) < 2:
            continue
        n = funder_proxy_count[funder]
        # Uniqueness weight: 1 / log2(n+1). 2 shared proxies → 0.63;
        # 100 shared (probably an exchange) → 0.15; 1000+ → ~0.10.
        uniq = 1.0 / math.log2(n + 1)
        for i in range(len(proxies)):
            for j in range(i + 1, len(proxies)):
                a, ai, av = proxies[i]
                b, bi, bv = proxies[j]
                key = tuple(sorted([a, b]))
                ent = pair_score.setdefault(
                    key,
                    {"score": 0.0, "shared_funders": [], "min_count": 999_999, "min_usdc": 0.0},
                )
                # Pair-level magnitude: min of the two sides (a transfer that
                # only flowed to one matters less than a sustained pattern)
                pair_mag = min(ai, bi)
                pair_score_inc = uniq * (1.0 - math.exp(-pair_mag / 5.0))
                ent["score"] += pair_score_inc
                ent["shared_funders"].append(
                    {"funder": funder, "uniqueness": round(uniq, 4), "min_transfers": pair_mag}
                )

    now = int(time.time())
    written = 0
    for (a, b), ent in pair_score.items():
        score = min(1.0, ent["score"])
        if score < 0.05:
            continue
        evidence = {
            "shared_funders": ent["shared_funders"][:10],
            "shared_funder_count": len(ent["shared_funders"]),
        }
        # Merge with any existing link (e.g. from trade-timing analyzer)
        existing = db.conn.execute(
            "SELECT score, reason, evidence_json FROM wallet_links WHERE address_a=? AND address_b=?",
            (a, b),
        ).fetchone()
        if existing:
            old_reasons = set((existing["reason"] or "").split(","))
            old_reasons.discard("")
            old_reasons.add("funding")
            new_reason = ",".join(sorted(old_reasons))
            try:
                old_ev = json.loads(existing["evidence_json"] or "{}")
            except json.JSONDecodeError:
                old_ev = {}
            old_ev["funding"] = evidence
            # Combine scores: 1 - (1-a)(1-b) so multiple signals reinforce
            combined = 1 - (1 - float(existing["score"])) * (1 - score)
            db.conn.execute(
                """
                UPDATE wallet_links SET score=?, reason=?, evidence_json=?, last_seen_ts=?
                 WHERE address_a=? AND address_b=?
                """,
                (combined, new_reason, json.dumps(old_ev), now, a, b),
            )
        else:
            db.conn.execute(
                """
                INSERT INTO wallet_links (address_a, address_b, score, reason,
                    evidence_json, first_seen_ts, last_seen_ts)
                VALUES (?,?,?,?,?,?,?)
                """,
                (a, b, score, "funding", json.dumps({"funding": evidence}), now, now),
            )
        written += 1
    log.info("funding-links rows written: %d", written)
    return written


# ----------------------------------------------------------------- helpers


def _wallets_to_scan(db: Database, *, limit: int) -> list[str]:
    """Top-PnL wallets + everything on the watchlist."""
    addrs: list[str] = []
    seen: set[str] = set()
    for r in db.conn.execute(
        "SELECT address FROM traders ORDER BY COALESCE(total_pnl_usdc, 0) DESC LIMIT ?",
        (limit,),
    ):
        a = r["address"]
        if a not in seen:
            addrs.append(a)
            seen.add(a)
    for r in db.conn.execute("SELECT address FROM watchlist WHERE active=1"):
        a = r["address"]
        if a not in seen:
            addrs.append(a)
            seen.add(a)
    return addrs


def _last_funding_block(db: Database, proxy: str) -> int | None:
    r = db.conn.execute(
        "SELECT MAX(block_number) FROM funding_transfers WHERE proxy_wallet=?",
        (proxy,),
    ).fetchone()
    return r[0] if r and r[0] is not None else None


def _refresh_wallet_funders(db: Database) -> None:
    db.conn.execute("DELETE FROM wallet_funders")
    db.conn.execute(
        """
        INSERT INTO wallet_funders (proxy_wallet, funder, first_ts, last_ts,
            transfer_cnt, total_usdc)
        SELECT proxy_wallet, funder,
               MIN(ts), MAX(ts), COUNT(*), SUM(amount_usdc)
          FROM funding_transfers
         GROUP BY proxy_wallet, funder
        """
    )
