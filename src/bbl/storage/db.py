"""Thin SQLite wrapper with schema bootstrap and typed helpers."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from importlib import resources
from pathlib import Path
from typing import Any, Iterable, Iterator


def _schema_sql() -> str:
    return resources.files("bbl.storage").joinpath("schema.sql").read_text()


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_schema_sql())
    return conn


class Database:
    """Bbl storage facade. Safe to share across async tasks (one conn)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.conn = connect(self.path)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- markets

    def upsert_markets(self, markets: Iterable[dict[str, Any]]) -> int:
        now = int(time.time())
        rows = 0
        with self.tx() as cur:
            for m in markets:
                cond = m.get("conditionId") or m.get("condition_id")
                if not cond:
                    continue
                cur.execute(
                    """
                    INSERT INTO markets (condition_id, question_id, slug, question,
                        description, category, event_id, end_date_iso,
                        active, closed, archived, volume_usdc, liquidity_usdc,
                        min_tick_size, raw_json, first_seen_ts, last_seen_ts)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(condition_id) DO UPDATE SET
                        question=excluded.question,
                        slug=excluded.slug,
                        active=excluded.active,
                        closed=excluded.closed,
                        archived=excluded.archived,
                        volume_usdc=excluded.volume_usdc,
                        liquidity_usdc=excluded.liquidity_usdc,
                        raw_json=excluded.raw_json,
                        last_seen_ts=excluded.last_seen_ts
                    """,
                    (
                        cond,
                        m.get("questionID") or m.get("question_id"),
                        m.get("slug"),
                        m.get("question"),
                        m.get("description"),
                        m.get("category"),
                        str(m.get("eventId") or (m.get("events") or [{}])[0].get("id", "")) or None,
                        m.get("endDate") or m.get("end_date_iso"),
                        int(bool(m.get("active", True))),
                        int(bool(m.get("closed", False))),
                        int(bool(m.get("archived", False))),
                        float(m.get("volumeNum") or m.get("volume") or 0),
                        float(m.get("liquidityNum") or m.get("liquidity") or 0),
                        float(m.get("minimumTickSize") or m.get("minTickSize") or 0) or None,
                        json.dumps(m, default=str),
                        now,
                        now,
                    ),
                )
                rows += 1
                for tok in _extract_tokens(m):
                    cur.execute(
                        """
                        INSERT INTO market_tokens (token_id, condition_id, outcome, outcome_index, winner)
                        VALUES (?,?,?,?,?)
                        ON CONFLICT(token_id) DO UPDATE SET
                            outcome=excluded.outcome,
                            winner=COALESCE(excluded.winner, market_tokens.winner)
                        """,
                        (
                            tok["token_id"],
                            cond,
                            tok.get("outcome"),
                            tok.get("outcome_index"),
                            tok.get("winner"),
                        ),
                    )
        return rows

    def active_markets(self, limit: int | None = None) -> list[sqlite3.Row]:
        q = (
            "SELECT * FROM markets WHERE active=1 AND closed=0 "
            "ORDER BY volume_usdc DESC"
        )
        if limit:
            q += f" LIMIT {int(limit)}"
        return self.conn.execute(q).fetchall()

    def active_token_ids(self, limit: int | None = None) -> list[str]:
        q = (
            "SELECT mt.token_id FROM market_tokens mt "
            "JOIN markets m ON m.condition_id = mt.condition_id "
            "WHERE m.active=1 AND m.closed=0 "
            "ORDER BY m.volume_usdc DESC"
        )
        if limit:
            q += f" LIMIT {int(limit)}"
        return [r[0] for r in self.conn.execute(q).fetchall()]

    # ------------------------------------------------------- prices / book

    def insert_price_snapshot(
        self,
        *,
        ts: int,
        token_id: str,
        mid: float | None = None,
        best_bid: float | None = None,
        best_ask: float | None = None,
        last_price: float | None = None,
    ) -> None:
        spread = (best_ask - best_bid) if (best_bid is not None and best_ask is not None) else None
        self.conn.execute(
            """
            INSERT OR REPLACE INTO price_snapshots
                (ts, token_id, mid, best_bid, best_ask, last_price, spread)
            VALUES (?,?,?,?,?,?,?)
            """,
            (ts, token_id, mid, best_bid, best_ask, last_price, spread),
        )

    def insert_orderbook_snapshot(
        self,
        *,
        ts: int,
        token_id: str,
        bids: list[list[float]],
        asks: list[list[float]],
    ) -> None:
        bid_depth = sum(float(s) for _, s in bids) if bids else 0.0
        ask_depth = sum(float(s) for _, s in asks) if asks else 0.0
        self.conn.execute(
            """
            INSERT OR REPLACE INTO orderbook_snapshots
                (ts, token_id, bids_json, asks_json, bid_depth, ask_depth)
            VALUES (?,?,?,?,?,?)
            """,
            (ts, token_id, json.dumps(bids), json.dumps(asks), bid_depth, ask_depth),
        )

    # ----------------------------------------------------------- trades

    def insert_trades(self, trades: Iterable[dict[str, Any]]) -> int:
        rows = 0
        with self.tx() as cur:
            for t in trades:
                tid = (
                    t.get("transactionHash")
                    or t.get("id")
                    or t.get("transaction_hash")
                )
                if not tid:
                    continue
                ts = _coerce_ts(t.get("timestamp") or t.get("ts") or t.get("time"))
                price = _f(t.get("price"))
                size = _f(t.get("size") or t.get("amount") or t.get("shares"))
                usdc = (price or 0) * (size or 0)
                cur.execute(
                    """
                    INSERT OR IGNORE INTO trades
                        (trade_id, ts, condition_id, token_id, outcome, side,
                         price, size, usdc_size, taker, maker, tx_hash, raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        str(tid),
                        ts,
                        t.get("conditionId") or t.get("market"),
                        t.get("asset") or t.get("tokenId") or t.get("token_id"),
                        t.get("outcome"),
                        (t.get("side") or "").upper() or None,
                        price,
                        size,
                        usdc,
                        (t.get("proxyWallet") or t.get("taker") or t.get("user") or "").lower() or None,
                        (t.get("maker") or "").lower() or None,
                        t.get("transactionHash"),
                        json.dumps(t, default=str),
                    ),
                )
                rows += cur.rowcount
        return rows

    # ----------------------------------------------------------- traders

    def upsert_traders(self, traders: Iterable[dict[str, Any]]) -> int:
        now = int(time.time())
        rows = 0
        with self.tx() as cur:
            for u in traders:
                addr = (u.get("proxyWallet") or u.get("address") or u.get("user") or "").lower()
                if not addr:
                    continue
                cur.execute(
                    """
                    INSERT INTO traders (address, username, display_name, profile_image, bio,
                        total_volume_usdc, total_pnl_usdc, trade_count,
                        first_seen_ts, last_seen_ts, raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(address) DO UPDATE SET
                        username=COALESCE(excluded.username, traders.username),
                        display_name=COALESCE(excluded.display_name, traders.display_name),
                        profile_image=COALESCE(excluded.profile_image, traders.profile_image),
                        total_volume_usdc=excluded.total_volume_usdc,
                        total_pnl_usdc=excluded.total_pnl_usdc,
                        trade_count=excluded.trade_count,
                        last_seen_ts=excluded.last_seen_ts,
                        raw_json=excluded.raw_json
                    """,
                    (
                        addr,
                        u.get("name") or u.get("username"),
                        u.get("displayName") or u.get("display_name"),
                        u.get("profileImage") or u.get("pseudonym"),
                        u.get("bio"),
                        _f(u.get("volume") or u.get("totalVolume") or u.get("total_volume")) or 0,
                        _f(u.get("pnl") or u.get("profit") or u.get("totalPnl")) or 0,
                        int(u.get("tradeCount") or u.get("trade_count") or 0),
                        now,
                        now,
                        json.dumps(u, default=str),
                    ),
                )
                rows += 1
        return rows

    def insert_leaderboard_snapshot(
        self,
        *,
        ts: int,
        window: str,
        metric: str,
        entries: Iterable[dict[str, Any]],
    ) -> int:
        rows = 0
        with self.tx() as cur:
            for rank, e in enumerate(entries, start=1):
                addr = (e.get("proxyWallet") or e.get("address") or e.get("user") or "").lower()
                if not addr:
                    continue
                value = _f(e.get("amount") or e.get("value") or e.get("profit") or e.get("volume")) or 0
                cur.execute(
                    """
                    INSERT OR REPLACE INTO leaderboard_snapshots
                        (ts, window, metric, rank, address, value)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (ts, window, metric, rank, addr, value),
                )
                rows += 1
        return rows

    # ----------------------------------------------------------- positions

    def insert_positions(self, ts: int, address: str, positions: Iterable[dict[str, Any]]) -> int:
        rows = 0
        with self.tx() as cur:
            for p in positions:
                cur.execute(
                    """
                    INSERT OR REPLACE INTO positions
                        (ts, address, condition_id, token_id, outcome,
                         size, avg_price, current_value, realized_pnl, unrealized_pnl)
                    VALUES (?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        ts,
                        address.lower(),
                        p.get("conditionId") or p.get("market"),
                        p.get("asset") or p.get("tokenId") or p.get("token_id"),
                        p.get("outcome"),
                        _f(p.get("size")),
                        _f(p.get("avgPrice") or p.get("avg_price")),
                        _f(p.get("currentValue") or p.get("current_value")),
                        _f(p.get("realizedPnl") or p.get("realized_pnl")),
                        _f(p.get("cashPnl") or p.get("unrealizedPnl") or p.get("unrealized_pnl")),
                    ),
                )
                rows += 1
        return rows

    # ------------------------------------------------------ collector log

    def log_run(
        self,
        *,
        collector: str,
        started_ts: int,
        finished_ts: int | None,
        status: str,
        rows_written: int = 0,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO collector_runs
                (collector, started_ts, finished_ts, status, rows_written, error)
            VALUES (?,?,?,?,?,?)
            """,
            (collector, started_ts, finished_ts, status, rows_written, error),
        )

    # ------------------------------------------------------- transactions

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Cursor]:
        cur = self.conn.cursor()
        cur.execute("BEGIN")
        try:
            yield cur
        except Exception:
            cur.execute("ROLLBACK")
            raise
        else:
            cur.execute("COMMIT")


# ---------------------------------------------------------- helpers


def _f(x: Any) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _coerce_ts(x: Any) -> int:
    """Accept unix seconds, ms, or ISO strings."""
    if x is None:
        return int(time.time())
    if isinstance(x, (int, float)):
        v = int(x)
        return v // 1000 if v > 10_000_000_000 else v
    if isinstance(x, str):
        try:
            v = int(float(x))
            return v // 1000 if v > 10_000_000_000 else v
        except ValueError:
            pass
        try:
            from dateutil.parser import isoparse

            return int(isoparse(x).timestamp())
        except Exception:
            return int(time.time())
    return int(time.time())


def _extract_tokens(m: dict[str, Any]) -> list[dict[str, Any]]:
    """Gamma returns tokens under various keys depending on endpoint version."""
    out: list[dict[str, Any]] = []
    outcomes = m.get("outcomes")
    token_ids = m.get("clobTokenIds") or m.get("tokens")
    if isinstance(outcomes, str):
        try:
            outcomes = json.loads(outcomes)
        except Exception:
            outcomes = None
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except Exception:
            token_ids = None
    if isinstance(token_ids, list) and token_ids and isinstance(token_ids[0], dict):
        # CLOB shape: [{"token_id": "...", "outcome": "Yes", "winner": false}, ...]
        for idx, t in enumerate(token_ids):
            tid = t.get("token_id") or t.get("tokenId")
            if not tid:
                continue
            out.append(
                {
                    "token_id": str(tid),
                    "outcome": t.get("outcome"),
                    "outcome_index": idx,
                    "winner": int(bool(t["winner"])) if "winner" in t else None,
                }
            )
        return out
    if isinstance(token_ids, list) and isinstance(outcomes, list):
        for idx, (tid, outcome) in enumerate(zip(token_ids, outcomes)):
            if not tid:
                continue
            out.append(
                {
                    "token_id": str(tid),
                    "outcome": outcome,
                    "outcome_index": idx,
                    "winner": None,
                }
            )
    return out
