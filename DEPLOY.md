# Deploy

The container runs collectors + Streamlit dashboard in one process.
Dashboard exposed on port `8501`.

## One-liner (Docker Compose)

```bash
# live mode (pulls from Polymarket)
docker compose up -d --build

# demo mode (synthetic data, no outbound calls)
BBL_MODE=demo docker compose up -d --build

# follow logs
docker compose logs -f bbl

# open dashboard
open http://localhost:8501
```

## Plain Docker

```bash
docker build -t bbl:latest .
docker run -d --name bbl -p 8501:8501 \
  -v bbl-data:/app/data \
  -e BBL_MODE=live \
  bbl:latest
```

## Environment variables

| var | default | meaning |
|-----|---------|---------|
| `BBL_MODE` | `live` | `live` = pull Polymarket APIs; `demo` = seed synthetic data |
| `BBL_COLLECTORS` | `markets,events,trades,leaderboard,prices,price_history,resolutions` | which collectors to run |
| `BBL_RUN_COLLECTORS` | `1` | set `0` for dashboard-only (read existing DB) |
| `BBL_BACKFILL_TOP` | `0` | `1` = kick a one-shot backfill of top wallets on boot |
| `BBL_BACKFILL_LIMIT` | `100` | how many top wallets to backfill |
| `BBL_BACKFILL_DAYS` | `30` | backfill lookback window |
| `BBL_POLYGON_RPC` | — | Polygon RPC for on-chain funding enrichment (optional) |
| `STREAMLIT_PORT` | `8501` | dashboard port inside container |

## Hosting options (any of these will work)

- **Fly.io** — `fly launch --dockerfile` then `fly deploy`. Persistent volume:
  `fly volumes create bbl_data --size 1`, mount at `/app/data`.
- **Railway / Render** — pick Dockerfile deploy, expose port `8501`,
  attach a volume to `/app/data`.
- **VPS (Hetzner/DO/etc.)** — `git clone`, `docker compose up -d`.
- **Kubernetes** — standard deployment + PVC on `/app/data`, service on `8501`.

## Persistence

SQLite DB + logs live in `/app/data`. Always mount a volume there or you'll
lose history on restart.

## Sanity check after deploy

```bash
docker compose exec bbl python -m bbl.cli status
# → row counts across all 22 tables, recent collector runs
```
