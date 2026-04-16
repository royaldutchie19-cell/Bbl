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

from bbl.analyzer import compute_trader_metrics, detect_patterns, find_wallet_links
from bbl.collectors import ALL_COLLECTORS
from bbl.config import Config
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
app.add_typer(collect_app, name="collect")
app.add_typer(markets_app, name="markets")
app.add_typer(analyze_app, name="analyze")
app.add_typer(leaderboard_app, name="leaderboard")
app.add_typer(watchlist_app, name="watchlist")

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
def analyze_all(config: str = typer.Option(None)) -> None:
    cfg, db = _setup(config)
    a = compute_trader_metrics(cfg, db)
    b = detect_patterns(cfg, db)
    c = find_wallet_links(cfg, db)
    console.print(
        f"[green]done[/green] — metrics:{a} patterns:{b} links:{c}"
    )
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


# ---------------------------------------------------------------- status


@app.command()
def status(config: str = typer.Option(None)) -> None:
    """Show counts and most-recent collector runs."""
    cfg, db = _setup(config)
    counts = {}
    for tbl in ("markets", "market_tokens", "price_snapshots",
                "orderbook_snapshots", "trades", "traders",
                "leaderboard_snapshots", "positions",
                "trader_metrics", "wallet_links", "watchlist"):
        counts[tbl] = db.conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
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


if __name__ == "__main__":
    app()
