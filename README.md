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

## Quick start

```bash
# 1. create DB + data dir
bbl init

# 2. one-shot: pull markets + leaderboard + recent trades
bbl collect once --collectors markets,trades,leaderboard

# 3. long-running: three parallel "runs" on independent schedules
bbl collect run --collectors markets,trades,leaderboard

# 4. see what's in the DB
bbl status
bbl markets list --limit 20
bbl leaderboard top --metric profit --window all

# 5. analyze (after you've collected some data)
bbl analyze all
```

## Running multiple collectors in parallel

All five collectors can run concurrently in a single process — each has its
own polling interval in `config.yaml`. You can also split them across shells
(three terminals = three "runs"):

```bash
# terminal 1 — catalog + leaderboard (slow cadence)
bbl collect run --collectors markets,leaderboard

# terminal 2 — trades firehose
bbl collect run --collectors trades

# terminal 3 — price + orderbook snapshots
bbl collect run --collectors prices,orderbooks
```

Each uses its own HTTP session and rate limiter, so they won't stomp on each
other. All write to the same SQLite DB (WAL mode).

## Analyzer

Three stages, meant to be run in this order:

1. `bbl analyze metrics` — compute per-wallet performance (PnL, ROI, win-rate,
   Sharpe-like, holding time, drawdown, early-entry score, markets traded).
   FIFO accounting from observed fills; resolved markets settle at {0, 1}.

2. `bbl analyze patterns` — annotate each top trader with category mix, bet
   sizing concentration (Gini), hour-of-day peak, reaction-time-after-move,
   and a contrarian-vs-momentum score based on trade price vs. mid at the
   time of the fill.

3. `bbl analyze links` — pairwise wallet linking. For each profitable wallet,
   find counterparties that repeatedly trade the same token/side within a
   short window, then combine with behavioral fingerprint similarity (hour
   distribution, log-size distribution) and a new-wallet prior. Writes to
   `wallet_links` with a reason-tag string and evidence JSON.

The output `wallet_links` is the main thing a copytrade bot should consume:
*"Wallet X is probably a fresh identity of known-top wallet Y with score 0.78
(reasons: timing, fingerprint, new_wallet) — backed by 34 overlapping fills."*

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

## Next steps (not yet implemented)

- On-chain funding-graph enrichment (RPC → wallet_links score boost)
- Copytrade executor (signing orders via `py-clob-client` + position sizing)
- Notification layer (fresh high-score wallet_link → webhook)
- Backtest harness over historical `price_snapshots` + `trades`
