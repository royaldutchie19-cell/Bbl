"""Streamlit dashboard for Bbl.

Run with:  bbl dashboard
or directly:  streamlit run src/bbl/dashboard.py

Reads straight from the same SQLite the collectors write to. Safe to run
while collectors are going — WAL mode + read-only queries here.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from bbl.config import Config


# ----------------------------------------------------------------- setup

st.set_page_config(
    page_title="Bbl — Polymarket Analyzer",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)


@st.cache_resource
def _conn(db_path: str) -> sqlite3.Connection:
    # check_same_thread=False — Streamlit reruns can hop threads
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@st.cache_data(ttl=30)
def _q(db_path: str, sql: str, params: tuple = ()) -> pd.DataFrame:
    conn = _conn(db_path)
    return pd.read_sql_query(sql, conn, params=params)


def _fmt_addr(addr: str | None, n: int = 8) -> str:
    if not addr:
        return ""
    return f"{addr[:n]}…{addr[-4:]}" if len(addr) > 14 else addr


def _human_ts(ts) -> str:
    if ts is None or pd.isna(ts):
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.gmtime(int(ts)))


# ------------------------------------------------------------ sidebar + routing

cfg = Config.load()
db_path = str(cfg.db_path)

if not Path(db_path).exists():
    st.error(f"DB not found at {db_path}. Run `bbl init` first.")
    st.stop()

PAGES = [
    "Overview",
    "Traders",
    "Leaderboard",
    "Markets",
    "Wallet",
    "Linked Wallets",
    "Collector Health",
]
page = st.sidebar.radio("View", PAGES)
st.sidebar.caption(f"DB: `{db_path}`")
st.sidebar.caption(f"Updated: {_human_ts(time.time())}")
if st.sidebar.button("Force refresh"):
    st.cache_data.clear()
    st.rerun()


# --------------------------------------------------------------- Overview

if page == "Overview":
    st.title("📈 Bbl — Polymarket Copytrade & Analyzer")
    st.caption("Market data + trader performance + wallet-link detection")

    tables = [
        ("markets", "Markets"),
        ("trades", "Trades"),
        ("traders", "Traders"),
        ("leaderboard_snapshots", "Leaderboard rows"),
        ("trader_metrics", "Analyzed traders"),
        ("wallet_links", "Suspected links"),
        ("funding_transfers", "Funding txs"),
    ]
    cols = st.columns(len(tables))
    for (tbl, label), col in zip(tables, cols):
        try:
            n = _q(db_path, f"SELECT COUNT(*) AS c FROM {tbl}").iloc[0]["c"]
        except Exception:
            n = 0
        col.metric(label, f"{int(n):,}")

    left, right = st.columns(2)

    with left:
        st.subheader("Top markets by volume")
        df = _q(
            db_path,
            """
            SELECT condition_id, question, category, volume_usdc, liquidity_usdc
              FROM markets WHERE active=1 AND closed=0
             ORDER BY volume_usdc DESC LIMIT 15
            """,
        )
        if not df.empty:
            df["question"] = df["question"].fillna("").str.slice(0, 80)
            st.dataframe(
                df[["question", "category", "volume_usdc", "liquidity_usdc"]],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No markets yet — run `bbl collect once --collectors markets`.")

    with right:
        st.subheader("Recent collector runs")
        df = _q(
            db_path,
            """
            SELECT collector, started_ts, status, rows_written, error
              FROM collector_runs ORDER BY id DESC LIMIT 15
            """,
        )
        if not df.empty:
            df["started"] = df["started_ts"].apply(_human_ts)
            st.dataframe(
                df[["collector", "started", "status", "rows_written", "error"]],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("No collector runs yet.")

    st.subheader("Trades over time")
    df = _q(
        db_path,
        """
        SELECT (ts / 3600) * 3600 AS bucket_ts,
               COUNT(*) AS trades,
               SUM(usdc_size) AS volume
          FROM trades
         WHERE ts > ?
         GROUP BY bucket_ts
         ORDER BY bucket_ts
        """,
        (int(time.time() - 7 * 86400),),
    )
    if not df.empty:
        df["time"] = pd.to_datetime(df["bucket_ts"], unit="s", utc=True)
        fig = px.area(df, x="time", y="volume", title="USDC volume (last 7 days, hourly)")
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=40, b=0))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No trade history yet — run `bbl backfill top` and/or `bbl collect run`.")


# ---------------------------------------------------------------- Traders

elif page == "Traders":
    st.title("Trader performance")
    c1, c2, c3 = st.columns([2, 2, 2])
    sort_by = c1.selectbox(
        "Sort by",
        ["realized_pnl", "roi", "sharpe_like", "win_rate", "volume_usdc", "trade_count"],
        index=0,
    )
    min_trades = c2.number_input("Min trades", 0, 10000, 20, 10)
    limit = c3.number_input("Rows", 10, 2000, 100, 10)

    df = _q(
        db_path,
        f"""
        SELECT tm.address, t.username, tm.trade_count, tm.volume_usdc,
               tm.realized_pnl, tm.roi, tm.win_rate, tm.sharpe_like,
               tm.markets_traded, tm.avg_trade_size, tm.max_drawdown,
               tm.early_entry_score, tm.contrarian_score, tm.notes
          FROM trader_metrics tm
          LEFT JOIN traders t ON t.address = tm.address
         WHERE tm.trade_count >= ?
         ORDER BY tm.{sort_by} DESC
         LIMIT ?
        """,
        (int(min_trades), int(limit)),
    )
    if df.empty:
        st.info("No analyzed traders yet — run `bbl analyze all`.")
    else:
        df["short"] = df["address"].apply(_fmt_addr)
        col_order = [
            "short", "username", "trade_count", "volume_usdc", "realized_pnl",
            "roi", "win_rate", "sharpe_like", "markets_traded",
            "avg_trade_size", "max_drawdown", "early_entry_score", "contrarian_score",
        ]
        st.dataframe(
            df[col_order].style.format({
                "volume_usdc": "${:,.0f}",
                "realized_pnl": "${:+,.0f}",
                "roi": "{:+.1%}",
                "win_rate": "{:.0%}",
                "sharpe_like": "{:.2f}",
                "avg_trade_size": "${:,.0f}",
                "max_drawdown": "${:,.0f}",
                "early_entry_score": "{:.2f}",
                "contrarian_score": "{:+.2f}",
            }),
            use_container_width=True,
            hide_index=True,
            height=520,
        )

        st.subheader("PnL distribution")
        fig = px.histogram(df, x="realized_pnl", nbins=40, title=None)
        fig.update_layout(height=280, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("ROI vs. volume (bubble = trade count)")
        fig2 = px.scatter(
            df, x="volume_usdc", y="roi", size="trade_count", hover_data=["short", "username"],
            log_x=True,
        )
        fig2.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig2, use_container_width=True)


# -------------------------------------------------------------- Leaderboard

elif page == "Leaderboard":
    st.title("Leaderboard")
    c1, c2, c3 = st.columns(3)
    metric = c1.selectbox("Metric", ["profit", "volume"], 0)
    window = c2.selectbox("Window", ["day", "week", "month", "all"], 3)
    limit = c3.number_input("Rows", 5, 500, 50, 5)

    df = _q(
        db_path,
        """
        SELECT ls.rank, ls.address, ls.value, t.username, t.trade_count,
               tm.realized_pnl AS analyzed_pnl, tm.roi
          FROM leaderboard_snapshots ls
          LEFT JOIN traders t ON t.address = ls.address
          LEFT JOIN trader_metrics tm ON tm.address = ls.address
         WHERE ls.window = ? AND ls.metric = ?
           AND ls.ts = (SELECT MAX(ts) FROM leaderboard_snapshots WHERE window=? AND metric=?)
         ORDER BY ls.rank
         LIMIT ?
        """,
        (window, metric, window, metric, int(limit)),
    )
    if df.empty:
        st.info("No leaderboard data yet — run `bbl collect once --collectors leaderboard`.")
    else:
        df["short"] = df["address"].apply(_fmt_addr)
        st.dataframe(
            df[["rank", "short", "username", "value", "trade_count", "analyzed_pnl", "roi"]]
            .style.format({"value": "${:,.0f}", "analyzed_pnl": "${:+,.0f}", "roi": "{:+.1%}"}),
            use_container_width=True,
            hide_index=True,
            height=520,
        )

    st.subheader("Rank history for a wallet")
    addr = st.text_input("wallet address", "")
    if addr:
        df2 = _q(
            db_path,
            """
            SELECT ts, window, metric, rank, value FROM leaderboard_snapshots
             WHERE address = ? ORDER BY ts
            """,
            (addr.lower(),),
        )
        if df2.empty:
            st.info("No leaderboard rows for this address.")
        else:
            df2["time"] = pd.to_datetime(df2["ts"], unit="s", utc=True)
            fig = px.line(
                df2, x="time", y="rank", color="window", facet_row="metric",
                title=f"Rank history — {addr}", markers=True,
            )
            fig.update_yaxes(autorange="reversed")  # rank 1 on top
            st.plotly_chart(fig, use_container_width=True)


# ----------------------------------------------------------------- Markets

elif page == "Markets":
    st.title("Markets")
    c1, c2 = st.columns([3, 1])
    q_filter = c1.text_input("Filter on question", "")
    only_active = c2.checkbox("Active only", True)

    where = []
    params: list = []
    if only_active:
        where.append("active=1 AND closed=0")
    if q_filter:
        where.append("question LIKE ?")
        params.append(f"%{q_filter}%")
    clause = "WHERE " + " AND ".join(where) if where else ""
    df = _q(
        db_path,
        f"""
        SELECT condition_id, question, category, end_date_iso,
               volume_usdc, liquidity_usdc, active, closed
          FROM markets {clause}
         ORDER BY volume_usdc DESC LIMIT 300
        """,
        tuple(params),
    )
    if df.empty:
        st.info("No markets matching.")
    else:
        df["question"] = df["question"].fillna("")
        st.dataframe(
            df.style.format({"volume_usdc": "${:,.0f}", "liquidity_usdc": "${:,.0f}"}),
            use_container_width=True,
            hide_index=True,
            height=480,
        )

    st.subheader("Token price history")
    tid = st.text_input("token_id", "")
    if tid:
        df2 = _q(
            db_path,
            """
            SELECT ts, mid, best_bid, best_ask FROM price_snapshots
             WHERE token_id = ? ORDER BY ts
            """,
            (tid,),
        )
        if df2.empty:
            st.info("No price snapshots for this token yet.")
        else:
            df2["time"] = pd.to_datetime(df2["ts"], unit="s", utc=True)
            fig = px.line(df2, x="time", y=["mid", "best_bid", "best_ask"])
            fig.update_layout(height=400, margin=dict(l=0, r=0, t=10, b=0))
            st.plotly_chart(fig, use_container_width=True)


# ----------------------------------------------------------------- Wallet

elif page == "Wallet":
    st.title("Wallet deep-dive")
    addr = st.text_input("Wallet address (proxy wallet)", "").lower().strip()
    if not addr:
        st.caption("Paste a proxy wallet to inspect — usually starts with 0x.")
        st.stop()

    meta = _q(db_path, "SELECT * FROM traders WHERE address = ?", (addr,))
    metrics = _q(db_path, "SELECT * FROM trader_metrics WHERE address = ?", (addr,))

    cols = st.columns(4)
    if not metrics.empty:
        m = metrics.iloc[0]
        cols[0].metric("Trades", f"{int(m['trade_count']):,}")
        cols[1].metric("Volume", f"${m['volume_usdc']:,.0f}")
        cols[2].metric("Realized PnL", f"${m['realized_pnl']:+,.0f}")
        cols[3].metric("ROI", f"{(m['roi'] or 0)*100:+.1f}%")
        if not meta.empty and meta.iloc[0]["username"]:
            st.caption(f"Known as: {meta.iloc[0]['username']}")
        if m["notes"]:
            try:
                st.json(json.loads(m["notes"]))
            except Exception:
                st.text(m["notes"])
    else:
        st.info("No analyzer metrics yet for this wallet.")

    st.subheader("Recent trades")
    df_trades = _q(
        db_path,
        """
        SELECT t.ts, t.side, t.price, t.size, t.usdc_size, m.question, t.outcome
          FROM trades t LEFT JOIN markets m ON m.condition_id = t.condition_id
         WHERE t.taker = ? ORDER BY t.ts DESC LIMIT 100
        """,
        (addr,),
    )
    if not df_trades.empty:
        df_trades["time"] = df_trades["ts"].apply(_human_ts)
        df_trades["question"] = df_trades["question"].fillna("").str.slice(0, 80)
        st.dataframe(
            df_trades[["time", "side", "outcome", "price", "size", "usdc_size", "question"]]
            .style.format({"price": "{:.3f}", "size": "{:,.0f}", "usdc_size": "${:,.2f}"}),
            use_container_width=True,
            hide_index=True,
            height=360,
        )

    st.subheader("Cumulative PnL (approximate, from realized fills)")
    if not df_trades.empty:
        # Rough: for now, treat side=SELL as +usdc_size and side=BUY as -usdc_size on cost basis.
        df_cum = df_trades.sort_values("ts").copy()
        df_cum["signed"] = df_cum.apply(
            lambda r: r["usdc_size"] if (r["side"] or "").upper() == "SELL" else -r["usdc_size"], axis=1
        )
        df_cum["cum"] = df_cum["signed"].cumsum()
        df_cum["time"] = pd.to_datetime(df_cum["ts"], unit="s", utc=True)
        fig = px.line(df_cum, x="time", y="cum", title=None)
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    st.subheader("Suspected linked wallets")
    links = _q(
        db_path,
        """
        SELECT address_b AS other, score, reason, evidence_json FROM wallet_links WHERE address_a=?
        UNION
        SELECT address_a AS other, score, reason, evidence_json FROM wallet_links WHERE address_b=?
        ORDER BY score DESC LIMIT 25
        """,
        (addr, addr),
    )
    if links.empty:
        st.caption("None above threshold.")
    else:
        links["short"] = links["other"].apply(_fmt_addr)
        st.dataframe(
            links[["short", "score", "reason"]].style.format({"score": "{:.2f}"}),
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Funders")
    funders = _q(
        db_path,
        """
        SELECT funder, transfer_cnt, total_usdc, first_ts, last_ts
          FROM wallet_funders WHERE proxy_wallet=?
         ORDER BY total_usdc DESC LIMIT 20
        """,
        (addr,),
    )
    if funders.empty:
        st.caption("Run `bbl onchain funding` to pull Polygon RPC data for this wallet.")
    else:
        funders["short"] = funders["funder"].apply(_fmt_addr)
        funders["first"] = funders["first_ts"].apply(_human_ts)
        funders["last"] = funders["last_ts"].apply(_human_ts)
        st.dataframe(
            funders[["short", "transfer_cnt", "total_usdc", "first", "last"]]
            .style.format({"total_usdc": "${:,.2f}"}),
            use_container_width=True,
            hide_index=True,
        )


# ----------------------------------------------------------- Linked Wallets

elif page == "Linked Wallets":
    st.title("Suspected linked wallets")
    c1, c2, c3 = st.columns(3)
    min_score = c1.slider("Min score", 0.0, 1.0, 0.30, 0.05)
    reason = c2.selectbox(
        "Reason contains",
        ["(any)", "timing", "fingerprint", "funding", "new_wallet"],
        0,
    )
    limit = c3.number_input("Rows", 10, 2000, 100, 10)

    q = """
        SELECT wl.address_a, wl.address_b, wl.score, wl.reason, wl.evidence_json,
               ta.username AS name_a, tb.username AS name_b,
               tma.realized_pnl AS pnl_a, tmb.realized_pnl AS pnl_b
          FROM wallet_links wl
          LEFT JOIN traders ta ON ta.address = wl.address_a
          LEFT JOIN traders tb ON tb.address = wl.address_b
          LEFT JOIN trader_metrics tma ON tma.address = wl.address_a
          LEFT JOIN trader_metrics tmb ON tmb.address = wl.address_b
         WHERE wl.score >= ?
    """
    params: list = [float(min_score)]
    if reason != "(any)":
        q += " AND wl.reason LIKE ?"
        params.append(f"%{reason}%")
    q += " ORDER BY wl.score DESC LIMIT ?"
    params.append(int(limit))

    df = _q(db_path, q, tuple(params))
    if df.empty:
        st.info("No link rows above threshold.")
    else:
        df["a"] = df["address_a"].apply(_fmt_addr)
        df["b"] = df["address_b"].apply(_fmt_addr)
        st.dataframe(
            df[["score", "reason", "a", "name_a", "pnl_a", "b", "name_b", "pnl_b"]]
            .style.format({"score": "{:.2f}", "pnl_a": "${:+,.0f}", "pnl_b": "${:+,.0f}"}),
            use_container_width=True,
            hide_index=True,
            height=480,
        )

        st.subheader("Score distribution")
        fig = px.histogram(df, x="score", nbins=30)
        fig.update_layout(height=250, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

        if st.checkbox("Show evidence for top 10"):
            for _, r in df.head(10).iterrows():
                st.markdown(f"**{r['a']} ↔ {r['b']}** — score {r['score']:.2f}, reason {r['reason']}")
                try:
                    st.json(json.loads(r["evidence_json"] or "{}"))
                except Exception:
                    st.text(r["evidence_json"])


# -------------------------------------------------------- Collector Health

elif page == "Collector Health":
    st.title("Collector health")
    df = _q(
        db_path,
        """
        SELECT collector,
               COUNT(*) AS runs,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok_runs,
               SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS err_runs,
               MAX(started_ts) AS last_started,
               SUM(rows_written) AS total_rows
          FROM collector_runs
         GROUP BY collector
         ORDER BY last_started DESC
        """,
    )
    if df.empty:
        st.info("No collector runs yet.")
    else:
        df["last"] = df["last_started"].apply(_human_ts)
        st.dataframe(
            df[["collector", "runs", "ok_runs", "err_runs", "total_rows", "last"]],
            use_container_width=True,
            hide_index=True,
        )

    st.subheader("Recent errors")
    df2 = _q(
        db_path,
        """
        SELECT collector, started_ts, error FROM collector_runs
         WHERE status='error' ORDER BY id DESC LIMIT 25
        """,
    )
    if df2.empty:
        st.caption("No recent errors.")
    else:
        df2["when"] = df2["started_ts"].apply(_human_ts)
        st.dataframe(df2[["when", "collector", "error"]], use_container_width=True, hide_index=True)
