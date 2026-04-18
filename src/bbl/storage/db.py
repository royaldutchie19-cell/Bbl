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


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(markets)").fetchall()}
    for col, typedef in [
        ("volume_24h", "REAL DEFAULT 0"),
        ("yes_price", "REAL"),
        ("no_price", "REAL"),
    ]:
        if col not in cols:
            conn.execute(f"ALTER TABLE markets ADD COLUMN {col} {typedef}")


def connect(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(_schema_sql())
    _migrate(conn)
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
                yes_price, no_price = _parse_outcome_prices(m)
                cur.execute(
                    """
                    INSERT INTO markets (condition_id, question_id, slug, question,
                        description, category, event_id, end_date_iso,
                        active, closed, archived, volume_usdc, liquidity_usdc,
                        volume_24h, yes_price, no_price,
                        min_tick_size, raw_json, first_seen_ts, last_seen_ts)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(condition_id) DO UPDATE SET
                        question=excluded.question,
                        slug=excluded.slug,
                        active=excluded.active,
                        closed=excluded.closed,
                        archived=excluded.archived,
                        volume_usdc=excluded.volume_usdc,
                        liquidity_usdc=excluded.liquidity_usdc,
                        volume_24h=COALESCE(excluded.volume_24h, markets.volume_24h),
                        yes_price=COALESCE(excluded.yes_price, markets.yes_price),
                        no_price=COALESCE(excluded.no_price, markets.no_price),
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
                        float(m.get("volume24hr") or m.get("volume_24h") or 0) or None,
                        yes_price,
                        no_price,
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
                usdc = _f(t.get("usdcSize") or t.get("usdc_size"))
                if usdc is None:
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

    # ------------------------------------------------------ price history

    def insert_price_history(
        self,
        *,
        token_id: str,
        fidelity: int,
        candles: Iterable[dict[str, Any]],
    ) -> int:
        rows = 0
        with self.tx() as cur:
            for c in candles:
                ts = _coerce_ts(c.get("t") or c.get("timestamp") or c.get("ts"))
                price = _f(c.get("p") or c.get("price") or c.get("close"))
                cur.execute(
                    """
                    INSERT OR REPLACE INTO price_history
                      (token_id, fidelity, ts, price, open, high, low, close, volume)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        token_id,
                        int(fidelity),
                        ts,
                        price,
                        _f(c.get("o") or c.get("open")),
                        _f(c.get("h") or c.get("high")),
                        _f(c.get("l") or c.get("low")),
                        _f(c.get("c") or c.get("close")),
                        _f(c.get("v") or c.get("volume")),
                    ),
                )
                rows += 1
        return rows

    # ---------------------------------------------- per-user time series

    def insert_user_value_series(
        self, address: str, series: Iterable[dict[str, Any]]
    ) -> int:
        rows = 0
        with self.tx() as cur:
            for p in _normalize_series(series):
                cur.execute(
                    "INSERT OR REPLACE INTO user_value_series (address, ts, value_usdc) VALUES (?,?,?)",
                    (address.lower(), p["ts"], p["value"]),
                )
                rows += 1
        return rows

    def insert_user_pnl_series(
        self, address: str, series: Iterable[dict[str, Any]]
    ) -> int:
        rows = 0
        with self.tx() as cur:
            for p in _normalize_series(series):
                cur.execute(
                    "INSERT OR REPLACE INTO user_pnl_series (address, ts, pnl) VALUES (?,?,?)",
                    (address.lower(), p["ts"], p["value"]),
                )
                rows += 1
        return rows

    def insert_profit_loss(
        self, address: str, entries: Iterable[dict[str, Any]]
    ) -> int:
        rows = 0
        with self.tx() as cur:
            for p in entries:
                cid = p.get("conditionId") or p.get("market") or p.get("condition_id")
                if not cid:
                    continue
                cur.execute(
                    """
                    INSERT OR REPLACE INTO profit_loss
                        (address, condition_id, outcome, size, avg_price, cur_price,
                         initial_value, current_value, pnl, realized_pnl, raw_json)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        address.lower(),
                        cid,
                        p.get("outcome"),
                        _f(p.get("size")),
                        _f(p.get("avgPrice") or p.get("avg_price")),
                        _f(p.get("curPrice") or p.get("currentPrice") or p.get("cur_price")),
                        _f(p.get("initialValue") or p.get("initial_value")),
                        _f(p.get("currentValue") or p.get("current_value")),
                        _f(p.get("pnl")),
                        _f(p.get("realizedPnl") or p.get("realized_pnl")),
                        json.dumps(p, default=str),
                    ),
                )
                rows += 1
        return rows

    def insert_user_rewards(
        self, address: str, entries: Iterable[dict[str, Any]]
    ) -> int:
        rows = 0
        with self.tx() as cur:
            for e in entries:
                ts = _coerce_ts(e.get("timestamp") or e.get("ts") or e.get("date"))
                cur.execute(
                    """
                    INSERT OR REPLACE INTO user_rewards
                        (address, ts, source, amount_usdc, raw_json)
                    VALUES (?,?,?,?,?)
                    """,
                    (
                        address.lower(),
                        ts,
                        e.get("source") or e.get("type") or "reward",
                        _f(e.get("amount") or e.get("value") or e.get("usdc")),
                        json.dumps(e, default=str),
                    ),
                )
                rows += 1
        return rows

    def insert_market_holders(
        self, condition_id: str, ts: int, holders: Iterable[dict[str, Any]]
    ) -> int:
        rows = 0
        with self.tx() as cur:
            for h in holders:
                addr = (h.get("proxyWallet") or h.get("address") or h.get("user") or "").lower()
                if not addr:
                    continue
                cur.execute(
                    """
                    INSERT OR REPLACE INTO market_holders
                        (ts, condition_id, token_id, address, shares, usdc_value)
                    VALUES (?,?,?,?,?,?)
                    """,
                    (
                        ts,
                        condition_id,
                        h.get("asset") or h.get("tokenId") or h.get("token_id"),
                        addr,
                        _f(h.get("shares") or h.get("size")),
                        _f(h.get("value") or h.get("usdcValue") or h.get("usdc_value")),
                    ),
                )
                rows += 1
        return rows

    # ----------------------------------------------------------- events

    def upsert_events(self, events: Iterable[dict[str, Any]]) -> int:
        now = int(time.time())
        rows = 0
        with self.tx() as cur:
            for e in events:
                eid = str(e.get("id") or e.get("eventId") or e.get("event_id") or "")
                if not eid:
                    continue
                cur.execute(
                    """
                    INSERT INTO events (event_id, slug, title, description, category,
                        volume_usdc, liquidity_usdc, start_date, end_date,
                        active, closed, featured, raw_json, first_seen_ts, last_seen_ts)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        title=excluded.title,
                        description=excluded.description,
                        category=excluded.category,
                        volume_usdc=excluded.volume_usdc,
                        liquidity_usdc=excluded.liquidity_usdc,
                        active=excluded.active,
                        closed=excluded.closed,
                        featured=excluded.featured,
                        raw_json=excluded.raw_json,
                        last_seen_ts=excluded.last_seen_ts
                    """,
                    (
                        eid,
                        e.get("slug"),
                        e.get("title") or e.get("name"),
                        e.get("description"),
                        e.get("category"),
                        _f(e.get("volume") or e.get("volumeNum")) or 0,
                        _f(e.get("liquidity") or e.get("liquidityNum")) or 0,
                        e.get("startDate") or e.get("start_date"),
                        e.get("endDate") or e.get("end_date"),
                        int(bool(e.get("active", True))),
                        int(bool(e.get("closed", False))),
                        int(bool(e.get("featured", False))),
                        json.dumps(e, default=str),
                        now,
                        now,
                    ),
                )
                rows += 1
        return rows

    def upsert_tags(self, tags: Iterable[dict[str, Any]]) -> int:
        rows = 0
        with self.tx() as cur:
            for t in tags:
                tid = str(t.get("id") or t.get("slug") or "")
                if not tid:
                    continue
                cur.execute(
                    """
                    INSERT OR REPLACE INTO tags (tag_id, slug, label, raw_json)
                    VALUES (?,?,?,?)
                    """,
                    (tid, t.get("slug"), t.get("label"), json.dumps(t, default=str)),
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


def _normalize_series(series: Iterable[Any]) -> list[dict[str, Any]]:
    """Accept heterogeneous PnL/value responses and normalize to {ts, value}.

    Shapes we see in the wild:
        [{"t": 1700000000, "p": 123.4}]
        [{"timestamp": 1700000000, "value": 123.4}]
        {"history": [...]}  # wrapped
    """
    out: list[dict[str, Any]] = []
    if isinstance(series, dict):
        series = series.get("history") or series.get("data") or []
    for p in series or []:
        if not isinstance(p, dict):
            continue
        ts = _coerce_ts(p.get("t") or p.get("timestamp") or p.get("ts") or p.get("time"))
        val = _f(
            p.get("p")
            if "p" in p
            else (p.get("v") or p.get("value") or p.get("pnl") or p.get("amount"))
        )
        if ts is None or val is None:
            continue
        out.append({"ts": ts, "value": val})
    return out


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


def _parse_outcome_prices(m: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extract Yes/No probability from outcomePrices field."""
    raw = m.get("outcomePrices")
    if not raw:
        return None, None
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return None, None
    if isinstance(raw, list) and len(raw) >= 2:
        try:
            return float(raw[0]), float(raw[1])
        except (ValueError, TypeError):
            return None, None
    return None, None


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
