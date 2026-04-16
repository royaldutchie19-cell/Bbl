# Bbl — Polymarket copytrade bot & trader analyzer

Infrastructure for collecting Polymarket market/trader data and analyzing what
makes top traders successful — including detecting when established top wallets
spin up fresh ones.

## What's here

```
src/bbl/
├── clients/        # Gamma, CLOB, data-api async HTTP clients
├── storage/        # SQLite schema + typed helpers
├── collectors/     # markets, prices, orderbooks, trades, leaderboard
├── analyzer/       # performance metrics, patterns, wallet linking
├── orchestrator.py # run N collectors concurrently
├── config.py       # env + YAML config
└── cli.py          # typer CLI
```

## Install

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[analyze]"
```

## Dashboard

Streamlit UI with 7 pages — reads straight from the SQLite DB, safe to run
alongside live collectors (WAL mode, read-only queries):

```bash
pip install -e ".[dashboard]"
bbl dashboard            # → http://localhost:8501
bbl dashboard --port 9000
```

Pages:
- **Overview** — row counts, recent collector runs, 7-day volume chart
- **Traders** — sortable trader_metrics table + PnL histogram + ROI/volume bubble
- **Leaderboard** — current top, plus rank history chart for any wallet
- **Markets** — searchable market list + per-token price chart
- **Wallet** — deep-dive per wallet: metrics, trades, cumulative PnL,
  suspected linked wallets, funders
- **Linked Wallets** — score-filtered list with reason tag + evidence JSON
- **Collector Health** — runs/errors per collector, last-run timestamps

## Quick start

```bash
# 1. create DB + data dir
bbl init

# 2. one-shot: pull markets + leaderboard + recent trades
bbl collect once --collectors markets,trades,leaderboard

# 3. long-running: six parallel "runs" on independent schedules
bbl collect run --collectors markets,trades,leaderboard,prices,orderbooks,resolutions

# 4. backfill historical trades for top wallets (so the analyzer has data)
bbl backfill top --limit 200 --since-days 90

# 5. on-chain funding enrichment (needs Polygon RPC; public one works but slow)
bbl onchain funding --limit 100

# 6. analyze everything
bbl analyze all --include-funding

# 7. read the reports
bbl report traders --limit 25
bbl report links --min-score 0.4
bbl report wallet 0xabc...
bbl leaderboard top --metric profit --window all
```

## Running multiple collectors in parallel

All six collectors can run concurrently in a single process — each has its
own polling interval in `config.yaml`. You can also split them across shells
(three terminals = three "runs"):

```bash
# terminal 1 — catalog + leaderboard + resolutions (slow cadence)
bbl collect run --collectors markets,leaderboard,resolutions

# terminal 2 — trades firehose
bbl collect run --collectors trades

# terminal 3 — price + orderbook snapshots
bbl collect run --collectors prices,orderbooks
```

Each uses its own HTTP session and rate limiter, so they won't stomp on each
other. All write to the same SQLite DB (WAL mode).

## Analyzer

Four stages, meant to be run in this order (roughly):

1. `bbl analyze metrics` — compute per-wallet performance (PnL, ROI, win-rate,
   Sharpe-like, holding time, drawdown, early-entry score, markets traded).
   FIFO accounting from observed fills; resolved markets settle at {0, 1}.

2. `bbl analyze patterns` — annotate each top trader with category mix, bet
   sizing concentration (Gini), hour-of-day peak, reaction-time-after-move,
   and a contrarian-vs-momentum score based on trade price vs. mid at the
   time of the fill.

3. `bbl analyze links` — pairwise wallet linking based on **trading
   behavior**. For each profitable wallet, find counterparties that
   repeatedly trade the same token/side within a short window, then combine
   with behavioral fingerprint similarity (hour distribution, log-size
   distribution) and a new-wallet prior.

4. `bbl onchain funding` + `bbl analyze all --include-funding` — pull USDC
   inbound transfers from Polygon RPC, build a per-funder graph, and merge
   shared-funder pairs into `wallet_links`. Exchange hot wallets (funders
   that serve hundreds of proxies) are auto-down-weighted so the score
   reflects *meaningful* shared funders only. This is the strongest signal
   for detecting fresh wallets of known top traders.

The combined `wallet_links` is the main thing a copytrade bot should consume:
*"Wallet X is probably a fresh identity of known-top wallet Y with score 0.78
(reasons: timing,fingerprint,funding) — backed by 34 overlapping fills and
2 shared unique funders."*

## Backfill

Live collection only captures data from the moment you start. To give the
analyzer real history:

```bash
# one specific wallet, full history (up to 200 pages × 500 trades)
bbl backfill wallet 0xabc...

# top-200 profit leaderboard, last 90 days
bbl backfill top --limit 200 --since-days 90
```

Backfill is safe to run repeatedly — trades dedupe on `trade_id`.

## Watchlist

```bash
bbl watchlist add 0xabc... --label "smart money #1" --weight 1.0
bbl watchlist list
```

Trades for watchlisted wallets are fetched on every `trades` tick so you
never miss a fill, regardless of global-feed pagination.

## Config

Copy `config.example.yaml` to `config.yaml` and edit. Every field is
optional — defaults live in `src/bbl/config.py`.

For on-chain funding: set `BBL_POLYGON_RPC` env var (or `onchain.rpc_url`
in config.yaml) to a serious RPC endpoint if you're scanning many wallets.
Public RPCs rate-limit aggressively and have small `getLogs` block
windows; configurable via `onchain.log_block_step`.

## Next steps (not yet implemented)

- WebSocket feeds (trades + orderbook) in place of polling — lower latency,
  lower API footprint
- Copytrade executor (signing orders via `py-clob-client` + position sizing,
  slippage, watchlist weight)
- Notification layer (fresh high-score wallet_link → webhook)
- Backtest harness over historical `price_snapshots` + `trades`
