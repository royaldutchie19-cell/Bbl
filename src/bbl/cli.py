"""Command-line entrypoint.

Examples:
    bbl init
    bbl markets refresh
    bbl collect run --collectors markets,trades,leaderboard
    bbl collect once --collectors prices,orderbooks
    bbl analyze all
    bbl leaderboard top --metric profit --window all --limit 25
    bbl watchlist add 0xabc... --label "smart money"
    bbl status
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from bbl.analyzer import (
    build_funding_links,
    compute_trader_metrics,
    detect_patterns,
    fetch_funding_for_top_wallets,
    find_wallet_links,
)
from bbl.backfill import backfill_top_traders, backfill_wallet
from bbl.collectors import ALL_COLLECTORS
from bbl.config import Config
from bbl.enrich import enrich_market_holders, enrich_top_traders, enrich_wallet
from bbl.orchestrator import build_collectors, run_collectors_forever
from bbl.storage import Database

app = typer.Typer(
    help="Bbl — Polymarket copytrade bot & trader analyzer",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
collect_app = typer.Typer(help="Data collection")
markets_app = typer.Typer(help="Markets commands")
analyze_app = typer.Typer(help="Analyzer")
leaderboard_app = typer.Typer(help="Leaderboard")
watchlist_app = typer.Typer(help="Watchlist (wallets to copy/track)")
backfill_app = typer.Typer(help="Historical backfill")
enrich_app = typer.Typer(help="Per-wallet enrichment (positions, pnl, portfolio, rewards)")
onchain_app = typer.Typer(help="On-chain (Polygon) enrichment")
report_app = typer.Typer(help="Reports")
app.add_typer(collect_app, name="collect")
app.add_typer(markets_app, name="markets")
app.add_typer(analyze_app, name="analyze")
app.add_typer(leaderboard_app, name="leaderboard")
app.add_typer(watchlist_app, name="watchlist")
app.add_typer(backfill_app, name="backfill")
app.add_typer(enrich_app, name="enrich")
app.add_typer(onchain_app, name="onchain")
app.add_typer(report_app, name="report")

console = Console()


def _setup(cfg_path: str | None = None) -> tuple[Config, Database]:
    cfg = Config.load(cfg_path)
    logging.basicConfig(
        level=cfg.log_level,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, markup=False)],
    )
    return cfg, Database(cfg.db_path)


# ------------------------------------------------------------------- init


@app.command()
def init(config: str = typer.Option(None, help="Optional config.yaml path")) -> None:
    """Create the DB and data directory."""
    cfg, db = _setup(config)
    console.print(f"[green]DB initialized[/green] at {cfg.db_path}")
    db.close()


# ----------------------------------------------------------------- collect


@collect_app.command("once")
def collect_once(
    collectors: str = typer.Option(
        "markets,prices,trades,leaderboard",
        help="Comma-separated collector names",
    ),
    config: str = typer.Option(None),
) -> None:
    """Run a single tick of each collector, then exit."""
    cfg, db = _setup(config)
    names = [n.strip() for n in collectors.split(",") if n.strip()]
    cs = build_collectors(cfg, db, names)

    async def _run() -> None:
        for c in cs:
            res = await c.run_once()
            console.print(f"[cyan]{c.name}[/cyan] → {res.rows_written} rows")

    asyncio.run(_run())
    db.close()


@collect_app.command("run")
def collect_run(
    collectors: str = typer.Option(
        "markets,prices,trades,leaderboard",
        help="Comma-separated collector names (run concurrently, each on its own schedule)",
    ),
    config: str = typer.Option(None),
) -> None:
    """Run selected collectors forever, concurrently. Ctrl-C to stop."""
    cfg, db = _setup(config)
    names = [n.strip() for n in collectors.split(",") if n.strip()]
    cs = build_collectors(cfg, db, names)
    console.print(f"[green]running[/green] collectors: {', '.join(names)}")
    try:
        asyncio.run(run_collectors_forever(cs))
    except KeyboardInterrupt:
        pass
    finally:
        db.close()


@collect_app.command("list")
def collect_list() -> None:
    table = Table(title="Available collectors")
    table.add_column("name")
    table.add_column("class")
    for n, cls in ALL_COLLECTORS.items():
        table.add_row(n, cls.__name__)
    console.print(table)


# ---------------------------------------------------------------- markets


@markets_app.command("refresh")
def markets_refresh(config: str = typer.Option(None)) -> None:
    """One-shot: refresh markets catalog from Gamma + CLOB."""
    cfg, db = _setup(config)
    coll = ALL_COLLECTORS["markets"](cfg, db)
    res = asyncio.run(coll.run_once())
    console.print(f"[green]{res.rows_written}[/green] markets upserted")
    db.close()


@markets_app.command("list")
def markets_list(
    limit: int = typer.Option(25),
    config: str = typer.Option(None),
) -> None:
    cfg, db = _setup(config)
    table = Table(title=f"Top {limit} active markets by volume")
    table.add_column("cond_id", overflow="fold")
    table.add_column("question", overflow="fold")
    table.add_column("volume", justify="right")
    table.add_column("liquidity", justify="right")
    for r in db.active_markets(limit=limit):
        table.add_row(
            (r["condition_id"] or "")[:16] + "…",
            (r["question"] or "")[:80],
            f"{(r['volume_usdc'] or 0):,.0f}",
            f"{(r['liquidity_usdc'] or 0):,.0f}",
        )
    console.print(table)
    db.close()


# ---------------------------------------------------------------- analyze


@analyze_app.command("metrics")
def analyze_metrics(config: str = typer.Option(None)) -> None:
    cfg, db = _setup(config)
    n = compute_trader_metrics(cfg, db)
    console.print(f"[green]trader_metrics[/green]: {n} rows")
    db.close()


@analyze_app.command("patterns")
def analyze_patterns(config: str = typer.Option(None)) -> None:
    cfg, db = _setup(config)
    n = detect_patterns(cfg, db)
    console.print(f"[green]patterns[/green]: {n} traders annotated")
    db.close()


@analyze_app.command("links")
def analyze_links(config: str = typer.Option(None)) -> None:
    cfg, db = _setup(config)
    n = find_wallet_links(cfg, db)
    console.print(f"[green]wallet_links[/green]: {n} rows")
    db.close()


@analyze_app.command("all")
def analyze_all(
    include_funding: bool = typer.Option(False, help="Also fold funding-graph links into wallet_links"),
    config: str = typer.Option(None),
) -> None:
    cfg, db = _setup(config)
    a = compute_trader_metrics(cfg, db)
    b = detect_patterns(cfg, db)
    c = find_wallet_links(cfg, db)
    d = build_funding_links(cfg, db) if include_funding else 0
    console.print(
        f"[green]done[/green] — metrics:{a} patterns:{b} links:{c} funding:{d}"
    )
    db.close()


# ----------------------------------------------------------------- backfill


@backfill_app.command("wallet")
def backfill_one(
    address: str,
    pages: int = typer.Option(200, help="Max pages to walk back"),
    since_days: int = typer.Option(0, help="Stop after N days back (0=no cutoff)"),
    config: str = typer.Option(None),
) -> None:
    """Pull historical activity for a single wallet."""
    cfg, db = _setup(config)
    since = int(time.time() - since_days * 86400) if since_days else None
    n = asyncio.run(
        backfill_wallet(cfg, db, address, max_pages=pages, since_ts=since)
    )
    console.print(f"[green]{n}[/green] trades inserted for {address}")
    db.close()


@backfill_app.command("top")
def backfill_top(
    limit: int = typer.Option(100, help="How many top wallets to backfill"),
    metric: str = typer.Option("profit"),
    window: str = typer.Option("all"),
    since_days: int = typer.Option(90),
    config: str = typer.Option(None),
) -> None:
    """Backfill activity for the top-N wallets on a leaderboard."""
    cfg, db = _setup(config)
    n = asyncio.run(
        backfill_top_traders(
            cfg, db, limit=limit, metric=metric, window=window, since_days=since_days
        )
    )
    console.print(f"[green]{n}[/green] trades backfilled across top {limit}")
    db.close()


# ------------------------------------------------------------------ enrich


@enrich_app.command("wallet")
def enrich_one(
    address: str,
    no_positions: bool = typer.Option(False),
    no_pnl: bool = typer.Option(False),
    no_portfolio: bool = typer.Option(False),
    no_rewards: bool = typer.Option(False),
    config: str = typer.Option(None),
) -> None:
    """Deep-pull positions/pnl/portfolio/rewards for a single wallet."""
    cfg, db = _setup(config)
    stats = asyncio.run(
        enrich_wallet(
            cfg, db, address,
            positions=not no_positions,
            pnl=not no_pnl,
            portfolio=not no_portfolio,
            rewards=not no_rewards,
        )
    )
    console.print(f"[green]{address}[/green]: {stats}")
    db.close()


@enrich_app.command("top")
def enrich_top(
    limit: int = typer.Option(50),
    metric: str = typer.Option("profit"),
    window: str = typer.Option("all"),
    config: str = typer.Option(None),
) -> None:
    """Enrich the top-N wallets on a leaderboard in parallel."""
    cfg, db = _setup(config)
    totals = asyncio.run(
        enrich_top_traders(cfg, db, limit=limit, metric=metric, window=window)
    )
    console.print(f"[green]done[/green] totals: {totals}")
    db.close()


@enrich_app.command("holders")
def enrich_holders(
    limit: int = typer.Option(50),
    config: str = typer.Option(None),
) -> None:
    """Snapshot top holders for the N highest-volume active markets."""
    cfg, db = _setup(config)
    n = asyncio.run(enrich_market_holders(cfg, db, limit=limit))
    console.print(f"[green]{n}[/green] holder rows inserted")
    db.close()


# ----------------------------------------------------------------- onchain


@onchain_app.command("funding")
def onchain_funding(
    limit: int = typer.Option(100, help="How many top wallets to enrich"),
    config: str = typer.Option(None),
) -> None:
    """Pull USDC inbound transfers for top wallets via Polygon RPC."""
    cfg, db = _setup(config)
    n = asyncio.run(fetch_funding_for_top_wallets(cfg, db, limit=limit))
    console.print(f"[green]{n}[/green] funding transfers inserted")
    m = build_funding_links(cfg, db)
    console.print(f"[green]{m}[/green] funding links written")
    db.close()


@onchain_app.command("funders")
def onchain_funders(
    address: str,
    config: str = typer.Option(None),
) -> None:
    """Show known funders for a proxy wallet."""
    cfg, db = _setup(config)
    rows = db.conn.execute(
        """
        SELECT funder, transfer_cnt, total_usdc, first_ts, last_ts
          FROM wallet_funders WHERE proxy_wallet = ?
         ORDER BY total_usdc DESC LIMIT 50
        """,
        (address.lower(),),
    ).fetchall()
    table = Table(title=f"Funders of {address}")
    for col in ("funder", "transfers", "total USDC", "first", "last"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["funder"],
            str(r["transfer_cnt"]),
            f"{r['total_usdc']:,.2f}",
            time.strftime("%Y-%m-%d", time.gmtime(r["first_ts"])) if r["first_ts"] else "",
            time.strftime("%Y-%m-%d", time.gmtime(r["last_ts"])) if r["last_ts"] else "",
        )
    console.print(table)
    db.close()


# ------------------------------------------------------------------ report


@report_app.command("traders")
def report_traders(
    limit: int = typer.Option(20),
    sort_by: str = typer.Option("realized_pnl", help="realized_pnl|roi|sharpe_like|win_rate"),
    config: str = typer.Option(None),
) -> None:
    """Top traders with key metrics in one view."""
    cfg, db = _setup(config)
    valid = {"realized_pnl", "roi", "sharpe_like", "win_rate", "volume_usdc"}
    if sort_by not in valid:
        raise typer.BadParameter(f"sort_by must be one of {valid}")
    rows = db.conn.execute(
        f"""
        SELECT tm.*, t.username
          FROM trader_metrics tm
          LEFT JOIN traders t ON t.address = tm.address
         ORDER BY {sort_by} DESC
         LIMIT ?
        """,
        (limit,),
    ).fetchall()
    table = Table(title=f"Top {limit} traders by {sort_by}")
    for col in ("addr", "name", "trades", "vol", "pnl", "roi", "win%", "sharpe", "mkts", "early"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            r["address"][:10] + "…",
            (r["username"] or "")[:14],
            str(r["trade_count"]),
            f"{r['volume_usdc']:,.0f}",
            f"{r['realized_pnl']:+,.0f}",
            f"{(r['roi'] or 0) * 100:+.1f}%",
            f"{(r['win_rate'] or 0) * 100:.0f}%",
            f"{r['sharpe_like'] or 0:.2f}",
            str(r["markets_traded"]),
            f"{r['early_entry_score'] or 0:.2f}",
        )
    console.print(table)
    db.close()


@report_app.command("links")
def report_links(
    limit: int = typer.Option(30),
    min_score: float = typer.Option(0.3),
    reason: str = typer.Option(None, help="Filter on reason substring (timing|fingerprint|funding|new_wallet)"),
    config: str = typer.Option(None),
) -> None:
    """Suspected wallet pairs — most likely same person/operator."""
    cfg, db = _setup(config)
    q = """
        SELECT wl.address_a, wl.address_b, wl.score, wl.reason,
               ta.username AS name_a, tb.username AS name_b,
               tma.realized_pnl AS pnl_a, tmb.realized_pnl AS pnl_b
          FROM wallet_links wl
          LEFT JOIN traders ta ON ta.address = wl.address_a
          LEFT JOIN traders tb ON tb.address = wl.address_b
          LEFT JOIN trader_metrics tma ON tma.address = wl.address_a
          LEFT JOIN trader_metrics tmb ON tmb.address = wl.address_b
         WHERE wl.score >= ?
    """
    params: list = [min_score]
    if reason:
        q += " AND wl.reason LIKE ?"
        params.append(f"%{reason}%")
    q += " ORDER BY wl.score DESC LIMIT ?"
    params.append(limit)
    rows = db.conn.execute(q, params).fetchall()
    table = Table(title=f"Suspected linked wallets (≥{min_score:.2f})")
    for col in ("score", "reason", "wallet A", "PnL A", "wallet B", "PnL B"):
        table.add_column(col)
    for r in rows:
        table.add_row(
            f"{r['score']:.2f}",
            r["reason"],
            f"{r['address_a'][:10]}… {r['name_a'] or ''}".strip(),
            f"{r['pnl_a'] or 0:+,.0f}",
            f"{r['address_b'][:10]}… {r['name_b'] or ''}".strip(),
            f"{r['pnl_b'] or 0:+,.0f}",
        )
    console.print(table)
    db.close()


@report_app.command("wallet")
def report_wallet(
    address: str,
    config: str = typer.Option(None),
) -> None:
    """Detailed view for a single wallet."""
    cfg, db = _setup(config)
    addr = address.lower()
    m = db.conn.execute(
        "SELECT * FROM trader_metrics WHERE address=?", (addr,)
    ).fetchone()
    if not m:
        console.print(f"[yellow]no metrics for {addr} — run analyze first[/yellow]")
        db.close()
        return
    console.print(f"[bold]{addr}[/bold]")
    console.print(
        f"  trades={m['trade_count']}  volume=${m['volume_usdc']:,.0f}  "
        f"pnl=${m['realized_pnl']:+,.0f}  roi={(m['roi'] or 0)*100:+.1f}%  "
        f"win_rate={(m['win_rate'] or 0)*100:.0f}%  sharpe={m['sharpe_like'] or 0:.2f}"
    )
    notes = m["notes"]
    if notes:
        console.print(f"  patterns: {notes}")
    links = db.conn.execute(
        """
        SELECT address_b AS other, score, reason FROM wallet_links WHERE address_a=?
        UNION
        SELECT address_a AS other, score, reason FROM wallet_links WHERE address_b=?
        ORDER BY score DESC LIMIT 10
        """,
        (addr, addr),
    ).fetchall()
    if links:
        t = Table(title="Suspected linked wallets")
        for col in ("score", "reason", "wallet"):
            t.add_column(col)
        for r in links:
            t.add_row(f"{r['score']:.2f}", r["reason"], r["other"])
        console.print(t)
    db.close()


# ------------------------------------------------------------- leaderboard


@leaderboard_app.command("top")
def lb_top(
    metric: str = typer.Option("profit"),
    window: str = typer.Option("all"),
    limit: int = typer.Option(25),
    config: str = typer.Option(None),
) -> None:
    cfg, db = _setup(config)
    rows = db.conn.execute(
        """
        SELECT ls.rank, ls.address, ls.value, t.username, t.trade_count
          FROM leaderboard_snapshots ls
          LEFT JOIN traders t ON t.address = ls.address
         WHERE ls.window = ? AND ls.metric = ?
           AND ls.ts = (SELECT MAX(ts) FROM leaderboard_snapshots WHERE window=? AND metric=?)
         ORDER BY ls.rank
         LIMIT ?
        """,
        (window, metric, window, metric, limit),
    ).fetchall()
    table = Table(title=f"Leaderboard — {metric}/{window}")
    table.add_column("#", justify="right")
    table.add_column("address", overflow="fold")
    table.add_column("name")
    table.add_column(metric, justify="right")
    table.add_column("trades", justify="right")
    for r in rows:
        table.add_row(
            str(r["rank"]),
            r["address"],
            r["username"] or "",
            f"{r['value']:,.0f}",
            str(r["trade_count"] or 0),
        )
    console.print(table)
    db.close()


# ------------------------------------------------------------- watchlist


@watchlist_app.command("add")
def watch_add(
    address: str,
    label: str = typer.Option(None),
    weight: float = typer.Option(1.0),
    config: str = typer.Option(None),
) -> None:
    cfg, db = _setup(config)
    db.conn.execute(
        """
        INSERT INTO watchlist (address, label, added_ts, weight, active)
        VALUES (?,?,?,?,1)
        ON CONFLICT(address) DO UPDATE SET
            label=COALESCE(excluded.label, watchlist.label),
            weight=excluded.weight,
            active=1
        """,
        (address.lower(), label, int(time.time()), weight),
    )
    console.print(f"[green]added[/green] {address} to watchlist")
    db.close()


@watchlist_app.command("list")
def watch_list(config: str = typer.Option(None)) -> None:
    cfg, db = _setup(config)
    table = Table(title="Watchlist")
    for col in ("address", "label", "weight", "active", "added"):
        table.add_column(col)
    for r in db.conn.execute(
        "SELECT address, label, weight, active, added_ts FROM watchlist ORDER BY added_ts DESC"
    ):
        table.add_row(
            r["address"],
            r["label"] or "",
            f"{r['weight']:.2f}",
            "yes" if r["active"] else "no",
            time.strftime("%Y-%m-%d", time.gmtime(r["added_ts"])),
        )
    console.print(table)
    db.close()


@watchlist_app.command("remove")
def watch_remove(address: str, config: str = typer.Option(None)) -> None:
    cfg, db = _setup(config)
    db.conn.execute(
        "UPDATE watchlist SET active=0 WHERE address=?",
        (address.lower(),),
    )
    console.print(f"[yellow]disabled[/yellow] {address}")
    db.close()


# ---------------------------------------------------------------- doctor


@app.command()
def doctor(
    config: str = typer.Option(None),
    as_json: bool = typer.Option(True, "--json/--human",
                                 help="Dump JSON (easy to paste for support) or human table"),
) -> None:
    """Dump a diagnostic snapshot — paste this output when data looks off."""
    import json as _json
    import os as _os

    cfg, db = _setup(config)
    out: dict = {"bbl_env": {}, "counts": {}, "samples": {}, "ranges": {}, "runs": {}}

    for k in ("BBL_MODE", "BBL_COLLECTORS", "BBL_RUN_COLLECTORS",
              "BBL_BACKFILL_TOP", "BBL_BACKFILL_LIMIT", "BBL_BACKFILL_DAYS",
              "BBL_POLYGON_RPC"):
        v = _os.environ.get(k)
        if v is not None:
            out["bbl_env"][k] = v if "RPC" not in k else "<set>"
    out["bbl_env"]["db_path"] = str(cfg.db_path)
    out["bbl_env"]["db_exists"] = Path(cfg.db_path).exists()

    tables = (
        "markets", "market_tokens", "events", "tags",
        "price_snapshots", "price_history", "orderbook_snapshots",
        "trades", "traders", "leaderboard_snapshots",
        "positions", "market_holders",
        "user_value_series", "user_pnl_series", "user_rewards",
        "trader_metrics", "wallet_links",
        "funding_transfers", "wallet_funders",
        "watchlist", "collector_runs",
    )
    for tbl in tables:
        try:
            out["counts"][tbl] = db.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except Exception as e:
            out["counts"][tbl] = f"err: {e!s}"

    def _rows(sql: str, params: tuple = ()) -> list:
        try:
            return [dict(r) for r in db.conn.execute(sql, params).fetchall()]
        except Exception as e:
            return [{"error": str(e)}]

    out["samples"]["traders_by_pnl"] = _rows(
        "SELECT address, username, total_pnl_usdc, total_volume_usdc, trade_count "
        "FROM traders ORDER BY total_pnl_usdc DESC NULLS LAST LIMIT 5"
    )
    out["samples"]["trader_metrics_by_realized_pnl"] = _rows(
        "SELECT address, realized_pnl, roi, volume_usdc, trade_count, "
        "win_rate, markets_traded FROM trader_metrics "
        "ORDER BY realized_pnl DESC NULLS LAST LIMIT 5"
    )
    out["samples"]["latest_leaderboard_profit"] = _rows(
        "SELECT address, value, rank FROM leaderboard_snapshots "
        "WHERE metric='profit' AND window='all' "
        "ORDER BY ts DESC, rank ASC LIMIT 10"
    )
    out["samples"]["latest_leaderboard_volume"] = _rows(
        "SELECT address, value, rank FROM leaderboard_snapshots "
        "WHERE metric='volume' AND window='all' "
        "ORDER BY ts DESC, rank ASC LIMIT 5"
    )
    out["samples"]["sample_trades"] = _rows(
        "SELECT trade_id, ts, taker, maker, token_id, side, "
        "price, size, usdc_size FROM trades ORDER BY ts DESC LIMIT 3"
    )
    out["samples"]["wallet_links"] = _rows(
        "SELECT address_a, address_b, score, reason FROM wallet_links "
        "ORDER BY score DESC LIMIT 5"
    )

    tr_range = _rows(
        "SELECT MIN(ts) AS min_ts, MAX(ts) AS max_ts, "
        "COUNT(DISTINCT taker) AS takers FROM trades"
    )
    out["ranges"]["trades"] = tr_range[0] if tr_range else {}
    lb_range = _rows(
        "SELECT MIN(ts) AS min_ts, MAX(ts) AS max_ts, "
        "COUNT(DISTINCT ts) AS snapshots FROM leaderboard_snapshots"
    )
    out["ranges"]["leaderboard"] = lb_range[0] if lb_range else {}

    out["runs"]["per_collector_latest"] = _rows(
        "SELECT collector, MAX(started_ts) AS last_start, "
        "COUNT(*) AS runs, "
        "SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok, "
        "SUM(CASE WHEN status!='ok' THEN 1 ELSE 0 END) AS errors "
        "FROM collector_runs GROUP BY collector ORDER BY collector"
    )
    out["runs"]["recent_errors"] = _rows(
        "SELECT collector, started_ts, status, error FROM collector_runs "
        "WHERE status != 'ok' ORDER BY id DESC LIMIT 5"
    )

    db.close()

    if as_json:
        console.print_json(data=out)
    else:
        console.print(out)


# ---------------------------------------------------------------- status


@app.command()
def status(config: str = typer.Option(None)) -> None:
    """Show counts and most-recent collector runs."""
    cfg, db = _setup(config)
    counts = {}
    for tbl in (
        "markets", "market_tokens", "events", "tags",
        "price_snapshots", "price_history", "orderbook_snapshots",
        "trades", "traders", "leaderboard_snapshots",
        "positions", "market_holders",
        "user_value_series", "user_pnl_series", "user_rewards",
        "trader_metrics", "wallet_links",
        "funding_transfers", "wallet_funders",
        "watchlist",
    ):
        try:
            counts[tbl] = db.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        except Exception:
            counts[tbl] = 0
    t = Table(title="DB row counts")
    t.add_column("table")
    t.add_column("rows", justify="right")
    for k, v in counts.items():
        t.add_row(k, f"{v:,}")
    console.print(t)

    t2 = Table(title="Last 10 collector runs")
    for col in ("collector", "started", "status", "rows", "error"):
        t2.add_column(col)
    for r in db.conn.execute(
        "SELECT collector, started_ts, status, rows_written, error "
        "FROM collector_runs ORDER BY id DESC LIMIT 10"
    ):
        t2.add_row(
            r["collector"],
            time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(r["started_ts"])),
            r["status"] or "",
            str(r["rows_written"] or 0),
            (r["error"] or "")[:60],
        )
    console.print(t2)
    db.close()


# ---------------------------------------------------------------- dashboard


@app.command()
def dashboard(
    port: int = typer.Option(8501, help="Streamlit port"),
    host: str = typer.Option("localhost"),
    config: str = typer.Option(None),
) -> None:
    """Launch the Streamlit dashboard.

    Equivalent to: `streamlit run src/bbl/dashboard.py`.
    Requires the `dashboard` extras: `pip install -e ".[dashboard]"`.
    """
    import subprocess
    import sys

    try:
        import streamlit  # noqa: F401
    except ImportError:
        console.print("[red]streamlit not installed[/red] — run `pip install -e \".[dashboard]\"`")
        raise typer.Exit(code=1) from None

    _setup(config)  # ensures DB dir exists + logging
    module_path = Path(__file__).parent / "dashboard.py"
    cmd = [
        sys.executable, "-m", "streamlit", "run", str(module_path),
        "--server.port", str(port),
        "--server.address", host,
        "--browser.gatherUsageStats", "false",
    ]
    console.print(f"[green]launching dashboard[/green] → http://{host}:{port}")
    subprocess.run(cmd, check=False)


if __name__ == "__main__":
    app()
