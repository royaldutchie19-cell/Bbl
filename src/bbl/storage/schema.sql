-- Bbl SQLite schema
-- All monetary amounts are in USDC (6 decimals) but stored as REAL for ergonomics.
-- Prices are probabilities in [0, 1].

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------------ markets

CREATE TABLE IF NOT EXISTS markets (
    condition_id     TEXT PRIMARY KEY,        -- canonical market id on CTF
    question_id      TEXT,
    slug             TEXT,
    question         TEXT,
    description      TEXT,
    category         TEXT,
    event_id         TEXT,
    end_date_iso     TEXT,
    active           INTEGER DEFAULT 1,
    closed           INTEGER DEFAULT 0,
    archived         INTEGER DEFAULT 0,
    volume_usdc      REAL DEFAULT 0,
    liquidity_usdc   REAL DEFAULT 0,
    min_tick_size    REAL,
    raw_json         TEXT,                    -- full payload for later back-fill
    first_seen_ts    INTEGER NOT NULL,
    last_seen_ts     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_markets_active ON markets(active, closed);
CREATE INDEX IF NOT EXISTS idx_markets_volume ON markets(volume_usdc DESC);
CREATE INDEX IF NOT EXISTS idx_markets_event  ON markets(event_id);

CREATE TABLE IF NOT EXISTS market_tokens (
    token_id      TEXT PRIMARY KEY,           -- ERC1155 position token
    condition_id  TEXT NOT NULL REFERENCES markets(condition_id),
    outcome       TEXT,                       -- e.g. "Yes", "No", "Team A"
    outcome_index INTEGER,
    winner        INTEGER                     -- 1 if resolved winner, 0 if loser, NULL if unresolved
);
CREATE INDEX IF NOT EXISTS idx_tokens_condition ON market_tokens(condition_id);

-- --------------------------------------------------------------- price/book

CREATE TABLE IF NOT EXISTS price_snapshots (
    ts          INTEGER NOT NULL,             -- unix seconds
    token_id    TEXT NOT NULL,
    mid         REAL,
    best_bid    REAL,
    best_ask    REAL,
    last_price  REAL,
    spread      REAL,
    PRIMARY KEY (token_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_prices_ts ON price_snapshots(ts);

CREATE TABLE IF NOT EXISTS orderbook_snapshots (
    ts          INTEGER NOT NULL,
    token_id    TEXT NOT NULL,
    bids_json   TEXT NOT NULL,                -- [[price, size], ...]
    asks_json   TEXT NOT NULL,
    bid_depth   REAL,                         -- summed size on bid side
    ask_depth   REAL,
    PRIMARY KEY (token_id, ts)
);
CREATE INDEX IF NOT EXISTS idx_book_ts ON orderbook_snapshots(ts);

-- -------------------------------------------------------------------- trades

-- One row per fill. Maker/taker are the two wallets involved.
CREATE TABLE IF NOT EXISTS trades (
    trade_id        TEXT PRIMARY KEY,          -- tx hash or API id
    ts              INTEGER NOT NULL,
    condition_id    TEXT,
    token_id        TEXT,
    outcome         TEXT,
    side            TEXT,                      -- BUY or SELL from taker perspective
    price           REAL,
    size            REAL,                      -- shares
    usdc_size       REAL,                      -- price * size
    taker           TEXT,                      -- wallet addr, lowercased
    maker           TEXT,
    tx_hash         TEXT,
    raw_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts      ON trades(ts);
CREATE INDEX IF NOT EXISTS idx_trades_taker   ON trades(taker, ts);
CREATE INDEX IF NOT EXISTS idx_trades_maker   ON trades(maker, ts);
CREATE INDEX IF NOT EXISTS idx_trades_cond_ts ON trades(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_trades_token   ON trades(token_id, ts);

-- -------------------------------------------------------------- traders/wallets

CREATE TABLE IF NOT EXISTS traders (
    address            TEXT PRIMARY KEY,       -- lowercased wallet
    username           TEXT,
    display_name       TEXT,
    profile_image      TEXT,
    bio                TEXT,
    joined_ts          INTEGER,                -- first on-chain activity we saw
    total_volume_usdc  REAL DEFAULT 0,
    total_pnl_usdc     REAL DEFAULT 0,
    trade_count        INTEGER DEFAULT 0,
    first_seen_ts      INTEGER NOT NULL,
    last_seen_ts       INTEGER NOT NULL,
    raw_json           TEXT
);
CREATE INDEX IF NOT EXISTS idx_traders_pnl    ON traders(total_pnl_usdc DESC);
CREATE INDEX IF NOT EXISTS idx_traders_volume ON traders(total_volume_usdc DESC);

-- A flat leaderboard log, appended each refresh — lets us trend over time.
CREATE TABLE IF NOT EXISTS leaderboard_snapshots (
    ts       INTEGER NOT NULL,
    window   TEXT NOT NULL,                    -- day/week/month/all
    metric   TEXT NOT NULL,                    -- profit/volume
    rank     INTEGER NOT NULL,
    address  TEXT NOT NULL,
    value    REAL NOT NULL,
    PRIMARY KEY (ts, window, metric, rank)
);
CREATE INDEX IF NOT EXISTS idx_lb_address ON leaderboard_snapshots(address, ts);

-- ----------------------------------------------------------------- positions

CREATE TABLE IF NOT EXISTS positions (
    ts              INTEGER NOT NULL,
    address         TEXT NOT NULL,
    condition_id    TEXT,
    token_id        TEXT,
    outcome         TEXT,
    size            REAL,
    avg_price       REAL,
    current_value   REAL,
    realized_pnl    REAL,
    unrealized_pnl  REAL,
    PRIMARY KEY (ts, address, token_id)
);
CREATE INDEX IF NOT EXISTS idx_positions_addr ON positions(address, ts);

-- ---------------------------------------------------------------- analyzer

-- Derived metrics per trader, recomputed by the analyzer job.
CREATE TABLE IF NOT EXISTS trader_metrics (
    address              TEXT PRIMARY KEY,
    computed_ts          INTEGER NOT NULL,
    window_start_ts      INTEGER,
    window_end_ts        INTEGER,
    trade_count          INTEGER,
    volume_usdc          REAL,
    realized_pnl         REAL,
    roi                  REAL,
    win_rate             REAL,                 -- fraction of winning trades
    avg_trade_size       REAL,
    median_trade_size    REAL,
    max_drawdown         REAL,
    sharpe_like          REAL,                 -- pnl_mean / pnl_std
    markets_traded       INTEGER,
    avg_holding_secs     REAL,
    early_entry_score    REAL,                 -- how early vs. market lifetime they enter
    contrarian_score     REAL,                 -- trade vs. current market consensus
    notes                TEXT
);

-- Edges between wallets we suspect are the same person / closely linked.
CREATE TABLE IF NOT EXISTS wallet_links (
    address_a     TEXT NOT NULL,
    address_b     TEXT NOT NULL,
    score         REAL NOT NULL,               -- 0..1 confidence
    reason        TEXT NOT NULL,               -- comma-sep tags: timing,funding,tx_graph,...
    evidence_json TEXT,
    first_seen_ts INTEGER NOT NULL,
    last_seen_ts  INTEGER NOT NULL,
    PRIMARY KEY (address_a, address_b)
);
CREATE INDEX IF NOT EXISTS idx_links_score ON wallet_links(score DESC);

-- Wallets we want to copy-trade. Populated manually or by the analyzer.
CREATE TABLE IF NOT EXISTS watchlist (
    address      TEXT PRIMARY KEY,
    label        TEXT,
    added_ts     INTEGER NOT NULL,
    weight       REAL DEFAULT 1.0,             -- sizing multiplier for copytrade
    active       INTEGER DEFAULT 1,
    notes        TEXT
);

-- ----------------------------------------------------------- price history

-- OHLC candles from CLOB /prices-history. One row per (token, fidelity, ts).
-- fidelity is minutes-per-candle (1, 60, 360, 1440, ...). Price is
-- midpoint-based; some fields may be NULL depending on CLOB version.
CREATE TABLE IF NOT EXISTS price_history (
    token_id    TEXT    NOT NULL,
    fidelity    INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    price       REAL,
    open        REAL,
    high        REAL,
    low         REAL,
    close       REAL,
    volume      REAL,
    PRIMARY KEY (token_id, fidelity, ts)
);
CREATE INDEX IF NOT EXISTS idx_price_history_token ON price_history(token_id, ts);

-- ---------------------------------------------------- per-user time series

-- Portfolio value series (from data-api /portfolio-value).
CREATE TABLE IF NOT EXISTS user_value_series (
    address    TEXT    NOT NULL,
    ts         INTEGER NOT NULL,
    value_usdc REAL,
    PRIMARY KEY (address, ts)
);

-- PnL series (from data-api /pnl).
CREATE TABLE IF NOT EXISTS user_pnl_series (
    address TEXT    NOT NULL,
    ts      INTEGER NOT NULL,
    pnl     REAL,
    PRIMARY KEY (address, ts)
);

-- Rewards / earnings log per user.
CREATE TABLE IF NOT EXISTS user_rewards (
    address      TEXT    NOT NULL,
    ts           INTEGER NOT NULL,
    source       TEXT,                -- e.g. "liquidity", "trading", "referral"
    amount_usdc  REAL,
    raw_json     TEXT,
    PRIMARY KEY (address, ts, source)
);

-- Top holders per market (who owns the resolved tokens).
CREATE TABLE IF NOT EXISTS market_holders (
    ts            INTEGER NOT NULL,
    condition_id  TEXT    NOT NULL,
    token_id      TEXT,
    address       TEXT    NOT NULL,
    shares        REAL,
    usdc_value    REAL,
    PRIMARY KEY (ts, condition_id, address, token_id)
);
CREATE INDEX IF NOT EXISTS idx_holders_market ON market_holders(condition_id, ts);
CREATE INDEX IF NOT EXISTS idx_holders_addr   ON market_holders(address, ts);

-- ----------------------------------------------------------------- events

-- Events group related markets (e.g. a Super Bowl event has one market per
-- team outcome). Useful for the analyzer to see if a wallet specializes in
-- certain event types.
CREATE TABLE IF NOT EXISTS events (
    event_id       TEXT PRIMARY KEY,
    slug           TEXT,
    title          TEXT,
    description    TEXT,
    category       TEXT,
    volume_usdc    REAL,
    liquidity_usdc REAL,
    start_date     TEXT,
    end_date       TEXT,
    active         INTEGER DEFAULT 1,
    closed         INTEGER DEFAULT 0,
    featured       INTEGER DEFAULT 0,
    raw_json       TEXT,
    first_seen_ts  INTEGER NOT NULL,
    last_seen_ts   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);
CREATE INDEX IF NOT EXISTS idx_events_volume   ON events(volume_usdc DESC);

-- Gamma tags. Markets can belong to multiple tags.
CREATE TABLE IF NOT EXISTS tags (
    tag_id   TEXT PRIMARY KEY,
    slug     TEXT,
    label    TEXT,
    raw_json TEXT
);

-- ----------------------------------------------------------------- on-chain

-- Inbound USDC transfers to Polymarket proxy wallets. Used to recover the
-- funding source (the EOA that sent USDC into the proxy). When two proxies
-- share a funder, that's a strong wallet-linking signal.
CREATE TABLE IF NOT EXISTS funding_transfers (
    proxy_wallet  TEXT NOT NULL,         -- the Polymarket proxy receiving USDC
    funder        TEXT NOT NULL,         -- the EOA sending USDC
    block_number  INTEGER NOT NULL,
    tx_hash       TEXT NOT NULL,
    log_index     INTEGER NOT NULL,
    amount_usdc   REAL,
    ts            INTEGER,
    PRIMARY KEY (tx_hash, log_index)
);
CREATE INDEX IF NOT EXISTS idx_funding_proxy  ON funding_transfers(proxy_wallet, ts);
CREATE INDEX IF NOT EXISTS idx_funding_funder ON funding_transfers(funder, ts);

-- Once we have transfers, this is the derived view: per proxy_wallet, the
-- distinct set of funders. Stored so the analyzer doesn't need to recompute.
CREATE TABLE IF NOT EXISTS wallet_funders (
    proxy_wallet  TEXT NOT NULL,
    funder        TEXT NOT NULL,
    first_ts      INTEGER,
    last_ts       INTEGER,
    transfer_cnt  INTEGER,
    total_usdc    REAL,
    PRIMARY KEY (proxy_wallet, funder)
);

-- Append-only log of collector runs — useful for debugging + backfill gaps.
CREATE TABLE IF NOT EXISTS collector_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    collector    TEXT NOT NULL,
    started_ts   INTEGER NOT NULL,
    finished_ts  INTEGER,
    status       TEXT,                         -- ok, error
    rows_written INTEGER,
    error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_collector ON collector_runs(collector, started_ts);

-- --------------------------------------------------------- smart money

-- Smart money convergence signals: when N+ top wallets enter the same
-- market within a short window, that's a signal worth tracking.
CREATE TABLE IF NOT EXISTS smart_money_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             INTEGER NOT NULL,            -- when the signal was generated
    condition_id   TEXT NOT NULL,
    token_id       TEXT,
    direction      TEXT NOT NULL,               -- bullish / bearish
    signal_strength REAL NOT NULL,              -- 0..1
    trader_count   INTEGER NOT NULL,            -- how many smart wallets
    total_usdc     REAL,                        -- combined USDC in the move
    avg_entry_price REAL,
    traders_json   TEXT,                        -- JSON list of {address, usdc, price}
    note           TEXT
);
CREATE INDEX IF NOT EXISTS idx_sm_signals_ts   ON smart_money_signals(ts DESC);
CREATE INDEX IF NOT EXISTS idx_sm_signals_cond ON smart_money_signals(condition_id, ts);

-- --------------------------------------------------------- market scores

-- Composite market score, recomputed periodically by the analyzer.
CREATE TABLE IF NOT EXISTS market_scores (
    condition_id       TEXT PRIMARY KEY,
    computed_ts        INTEGER NOT NULL,
    volume_24h         REAL,
    volume_velocity    REAL,                    -- 24h vol / 7d avg daily vol
    smart_money_flow   REAL,                    -- net smart money USDC (pos=bullish)
    trader_influx      REAL,                    -- new unique traders in 24h
    spread_quality     REAL,                    -- 0..1, 1 = tightest
    holder_concentration REAL,                  -- gini of top holders
    composite_score    REAL,                    -- weighted combination, 0..1
    breakdown_json     TEXT                     -- per-dimension detail
);
CREATE INDEX IF NOT EXISTS idx_market_scores ON market_scores(composite_score DESC);
