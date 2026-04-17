#!/usr/bin/env bash
# Container entrypoint: init DB, optionally seed, start collectors + dashboard.
set -euo pipefail

: "${BBL_DATA_DIR:=/app/data}"
: "${BBL_MODE:=live}"                 # "live" | "demo"
: "${BBL_COLLECTORS:=markets,events,trades,leaderboard,prices,price_history,resolutions}"
: "${BBL_RUN_COLLECTORS:=1}"          # set to 0 to skip collectors (dashboard-only)
: "${BBL_BACKFILL_TOP:=1}"            # 1 = also backfill top N wallets on boot (recommended)
: "${BBL_BACKFILL_LIMIT:=100}"
: "${BBL_BACKFILL_DAYS:=30}"
: "${STREAMLIT_PORT:=8501}"

mkdir -p "$BBL_DATA_DIR"

echo "[entrypoint] bbl init"
python -m bbl.cli init || true

if [[ "$BBL_MODE" == "demo" ]]; then
    echo "[entrypoint] seeding synthetic demo data"
    python scripts/seed_demo.py
fi

if [[ "$BBL_MODE" == "live" && "$BBL_RUN_COLLECTORS" == "1" ]]; then
    echo "[entrypoint] starting collectors: $BBL_COLLECTORS"
    python -m bbl.cli collect run --collectors "$BBL_COLLECTORS" \
        >/app/data/collectors.log 2>&1 &
    COLLECTOR_PID=$!
    echo "[entrypoint] collectors pid=$COLLECTOR_PID (log: /app/data/collectors.log)"

    if [[ "$BBL_BACKFILL_TOP" == "1" ]]; then
        echo "[entrypoint] backfilling top $BBL_BACKFILL_LIMIT wallets (${BBL_BACKFILL_DAYS}d) + analyze after"
        (sleep 30 && python -m bbl.cli backfill top \
            --limit "$BBL_BACKFILL_LIMIT" --since-days "$BBL_BACKFILL_DAYS" \
            && echo "[backfill] done, running analyzer..." \
            && python -m bbl.cli analyze all --include-funding \
            >/app/data/backfill.log 2>&1) &
    fi
fi

echo "[entrypoint] launching dashboard on :$STREAMLIT_PORT"
exec python -m streamlit run src/bbl/dashboard.py \
    --server.port="$STREAMLIT_PORT" \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --browser.gatherUsageStats=false
