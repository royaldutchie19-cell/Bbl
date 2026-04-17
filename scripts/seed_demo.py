"""Seed the DB with synthetic data so the dashboard has something to render.

For when you can't pull live Polymarket data (e.g. sandboxed env).
Runs analyzer + builds wallet_links + funding_transfers too.
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bbl.analyzer import (
    build_funding_links,
    classify_traders,
    compute_trader_metrics,
    detect_patterns,
    detect_smart_money_signals,
    find_wallet_links,
    score_markets,
)
from bbl.analyzer.onchain_links import _refresh_wallet_funders
from bbl.config import Config
from bbl.storage import Database

random.seed(42)

cfg = Config.load()
db = Database(cfg.db_path)
now = int(time.time())


MARKETS = [
    ("will-trump-win-2024", "Will Trump win the 2024 election?", "politics", 15_000_000),
    ("superbowl-2026-kc",   "Will KC win Super Bowl LX?",         "sports",   3_200_000),
    ("btc-100k-eoy",        "BTC reaches $100k by end of year?",  "crypto",   8_750_000),
    ("eth-5k-q2",           "ETH above $5k in Q2?",               "crypto",   4_100_000),
    ("fed-cut-may",         "Fed rate cut in May?",               "macro",    2_400_000),
    ("oscars-best-pic",     "Best Picture Oscar winner?",         "culture",    900_000),
    ("champs-league",       "Champions League winner 2026?",      "sports",   1_800_000),
    ("uk-election",         "UK snap election before 2027?",      "politics",   650_000),
    ("ai-reg-eu",           "EU passes AI Act amendments 2026?",  "politics",   420_000),
    ("nba-finals",          "NBA finals winner 2026?",            "sports",   2_100_000),
]

# Markets + tokens (Yes/No each)
market_tokens = []
for slug, question, cat, vol in MARKETS:
    cid = "0x" + slug.replace("-", "")[:32].ljust(32, "a")
    y_tok = "t_" + slug + "_yes"
    n_tok = "t_" + slug + "_no"
    market_tokens.append((cid, y_tok, n_tok))
    db.upsert_markets([{
        "conditionId": cid,
        "slug": slug,
        "question": question,
        "category": cat,
        "volumeNum": vol,
        "liquidityNum": vol / 20,
        "active": True,
        "closed": False,
        "clobTokenIds": [
            {"token_id": y_tok, "outcome": "Yes", "winner": None},
            {"token_id": n_tok, "outcome": "No",  "winner": None},
        ],
    }])

# Price snapshots + price history (last 7 days, hourly)
for cid, y_tok, n_tok in market_tokens:
    base = random.uniform(0.2, 0.8)
    walk = base
    for h in range(7 * 24):
        ts = now - (7 * 24 - h) * 3600
        walk += random.gauss(0, 0.02)
        walk = max(0.02, min(0.98, walk))
        db.insert_price_snapshot(
            ts=ts, token_id=y_tok, mid=walk,
            best_bid=walk - 0.01, best_ask=walk + 0.01,
        )
        db.insert_price_snapshot(
            ts=ts, token_id=n_tok, mid=1 - walk,
            best_bid=1 - walk - 0.01, best_ask=1 - walk + 0.01,
        )
    db.insert_price_history(
        token_id=y_tok,
        fidelity=60,
        candles=[{"t": now - (7*24 - h) * 3600, "p": max(0.02, min(0.98, base + math.sin(h/8) * 0.1))} for h in range(7*24)],
    )

# Traders — 40 addresses, some profitable some not
ADDR_PREFIXES = [
    "whale", "degen", "alpha", "sharp", "kelly", "kelly2", "polymaxi",
    "newwallet1", "newwallet2", "insider", "flipkid", "mm_one", "mm_two",
]
addresses = []
for i in range(40):
    addresses.append("0x" + hex(random.getrandbits(160))[2:].rjust(40, "0")[:40])

# Named top wallets
named = {
    addresses[0]: "whale_alpha",
    addresses[1]: "degen_master",
    addresses[2]: "sharp_money",
    addresses[3]: "polymaxi",
    addresses[4]: "kelly_criterion",
}

# Seed traders table
for addr in addresses:
    db.upsert_traders([{
        "proxyWallet": addr,
        "name": named.get(addr),
        "volume": random.uniform(5_000, 500_000),
        "pnl": random.uniform(-20_000, 80_000),
        "tradeCount": random.randint(20, 400),
    }])

# Generate trades — top wallets are skewed toward winning positions
for i, addr in enumerate(addresses):
    is_top = i < 8
    n_trades = random.randint(30, 300) if is_top else random.randint(5, 60)
    for _ in range(n_trades):
        cid, y_tok, n_tok = random.choice(market_tokens)
        token_id = random.choice([y_tok, n_tok])
        ts = now - random.randint(60, 7 * 86400)
        # Top wallets tend to buy at better prices (lower for eventual winners)
        bias = -0.05 if is_top else 0.02
        price = max(0.03, min(0.97, random.uniform(0.2, 0.8) + bias))
        size = random.uniform(50, 2000) * (2 if is_top else 1)
        side = random.choices(["BUY", "SELL"], weights=[0.65, 0.35])[0]
        tid = f"0xtx{random.getrandbits(64):016x}"
        db.insert_trades([{
            "transactionHash": tid,
            "timestamp": ts,
            "conditionId": cid,
            "tokenId": token_id,
            "side": side,
            "price": price,
            "size": size,
            "usdcSize": price * size,
            "proxyWallet": addr,
        }])

# Mark some winners so PnL settles
for idx, (cid, y_tok, n_tok) in enumerate(market_tokens):
    if idx % 3 == 0:  # ~1/3 resolved
        winner = y_tok if random.random() < 0.5 else n_tok
        loser = n_tok if winner == y_tok else y_tok
        db.conn.execute("UPDATE market_tokens SET winner=1 WHERE token_id=?", (winner,))
        db.conn.execute("UPDATE market_tokens SET winner=0 WHERE token_id=?", (loser,))

# Leaderboard snapshots (current + yesterday)
for ts_offset in (0, 86400, 2 * 86400):
    entries_profit = [
        {"proxyWallet": addr, "value": random.uniform(5_000, 200_000)}
        for addr in addresses[:20]
    ]
    entries_profit.sort(key=lambda e: e["value"], reverse=True)
    db.insert_leaderboard_snapshot(
        ts=now - ts_offset, window="all", metric="profit",
        entries=entries_profit,
    )
    entries_vol = [
        {"proxyWallet": addr, "value": random.uniform(50_000, 2_000_000)}
        for addr in addresses[:25]
    ]
    entries_vol.sort(key=lambda e: e["value"], reverse=True)
    db.insert_leaderboard_snapshot(
        ts=now - ts_offset, window="all", metric="volume",
        entries=entries_vol,
    )

# Funding transfers — set up some shared-funder scenarios
# whale_alpha (addr 0) and newwallet1 (addr 7) share a unique funder.
# degen_master (addr 1) and newwallet2 (addr 8) share another unique funder.
# A bunch of wallets share "binance_hot" -- should be auto-down-weighted.
binance = "0x" + "b" * 40
unique_funder_1 = "0x" + "1" * 40
unique_funder_2 = "0x" + "2" * 40

funding_rows = []
# whale_alpha + newwallet1 from unique_funder_1
for i, addr in enumerate([addresses[0], addresses[7]]):
    for j in range(5):
        funding_rows.append((
            addr, unique_funder_1,
            30_000_000 + i * 10 + j,
            f"0xtf1{i}{j}".ljust(66, "0"),
            j,
            random.uniform(500, 5000),
            now - random.randint(86400, 30*86400),
        ))
# degen_master + newwallet2 from unique_funder_2
for i, addr in enumerate([addresses[1], addresses[8]]):
    for j in range(3):
        funding_rows.append((
            addr, unique_funder_2,
            30_000_000 + i * 10 + j + 100,
            f"0xtf2{i}{j}".ljust(66, "0"),
            j,
            random.uniform(200, 3000),
            now - random.randint(86400, 20*86400),
        ))
# Many wallets share binance_hot
for i, addr in enumerate(addresses[:15]):
    funding_rows.append((
        addr, binance,
        20_000_000 + i,
        f"0xtfb{i}".ljust(66, "0"),
        0,
        random.uniform(100, 20_000),
        now - random.randint(86400, 60*86400),
    ))

with db.tx() as cur:
    for r in funding_rows:
        cur.execute(
            "INSERT OR IGNORE INTO funding_transfers VALUES (?,?,?,?,?,?,?)", r
        )
_refresh_wallet_funders(db)

# Fake watchlist
db.conn.execute(
    "INSERT OR IGNORE INTO watchlist (address, label, added_ts, weight, active) VALUES (?,?,?,?,1)",
    (addresses[0], "whale_alpha (top-1 all-time)", now, 1.0),
)
db.conn.execute(
    "INSERT OR IGNORE INTO watchlist (address, label, added_ts, weight, active) VALUES (?,?,?,?,1)",
    (addresses[1], "degen_master", now, 0.5),
)

# Run analyzer pipeline
print("compute_trader_metrics:", compute_trader_metrics(cfg, db))
print("detect_patterns:", detect_patterns(cfg, db))
print("classify_traders:", classify_traders(cfg, db))
print("detect_smart_money_signals:", detect_smart_money_signals(cfg, db))
print("score_markets:", score_markets(cfg, db))
print("find_wallet_links:", find_wallet_links(cfg, db))
print("build_funding_links:", build_funding_links(cfg, db))

# Collector runs log (just for UI completeness)
for coll in ("markets", "leaderboard", "trades", "prices", "resolutions"):
    db.log_run(
        collector=coll,
        started_ts=now - random.randint(60, 3600),
        finished_ts=now - random.randint(0, 30),
        status="ok",
        rows_written=random.randint(50, 2000),
    )

print("\nSeed complete. Tables:")
for tbl in ("markets", "trades", "traders", "leaderboard_snapshots",
            "trader_metrics", "wallet_links", "funding_transfers",
            "price_history", "wallet_funders",
            "smart_money_signals", "market_scores"):
    n = db.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
    print(f"  {tbl}: {n}")

db.close()
