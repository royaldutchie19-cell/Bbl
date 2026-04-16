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
