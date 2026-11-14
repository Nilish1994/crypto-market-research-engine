"""SQLite storage for the market-data recorder (data/market_data.db, separate from trades.db)."""
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Closed candles only. Times are Binance ms since epoch, UTC.
-- volume = BTC (base asset, what TradingView shows); quote_volume = USDT.
CREATE TABLE IF NOT EXISTS candles (
    symbol                 TEXT    NOT NULL,
    timeframe              TEXT    NOT NULL,
    open_time              INTEGER NOT NULL,
    close_time             INTEGER NOT NULL,
    open                   REAL    NOT NULL,
    high                   REAL    NOT NULL,
    low                    REAL    NOT NULL,
    close                  REAL    NOT NULL,
    volume                 REAL    NOT NULL,
    quote_volume           REAL    NOT NULL,
    trades                 INTEGER NOT NULL,
    taker_buy_volume       REAL    NOT NULL,
    taker_buy_quote_volume REAL    NOT NULL,
    PRIMARY KEY (symbol, timeframe, open_time)
);

-- One row per candle. NULL = not enough history yet (Pine na).
-- rsi_avg_gain / rsi_avg_loss are kept so updates can continue the RSI exactly.
CREATE TABLE IF NOT EXISTS indicators (
    symbol       TEXT    NOT NULL,
    timeframe    TEXT    NOT NULL,
    open_time    INTEGER NOT NULL,
    ema50        REAL,
    ema200       REAL,
    rsi14        REAL,
    rsi_avg_gain REAL,
    rsi_avg_loss REAL,
    volume_sma20 REAL,
    bars_seen    INTEGER NOT NULL,
    PRIMARY KEY (symbol, timeframe, open_time)
);

-- Strategy-1 signals (LR50, LR200, SR50, SR200) on the candle that closed.
CREATE TABLE IF NOT EXISTS signals (
    symbol     TEXT    NOT NULL,
    timeframe  TEXT    NOT NULL,
    open_time  INTEGER NOT NULL,
    signal     TEXT    NOT NULL,
    direction  TEXT    NOT NULL,
    close      REAL    NOT NULL,
    ema        REAL    NOT NULL,          -- the EMA that was retested
    PRIMARY KEY (symbol, timeframe, open_time, signal)
);

-- For each signal: the latest candle of every other timeframe that had CLOSED
-- by the time the signal candle closed (no look-ahead).
CREATE TABLE IF NOT EXISTS signal_context (
    symbol         TEXT    NOT NULL,
    timeframe      TEXT    NOT NULL,
    open_time      INTEGER NOT NULL,
    signal         TEXT    NOT NULL,
    context_tf     TEXT    NOT NULL,
    ctx_open_time  INTEGER NOT NULL,
    ctx_close      REAL    NOT NULL,
    ctx_ema50      REAL,
    ctx_ema200     REAL,
    ctx_rsi14      REAL,
    ctx_trend      TEXT,                  -- UP / DOWN / NULL (not warmed up)
    PRIMARY KEY (symbol, timeframe, open_time, signal, context_tf)
);

-- What price did in the N candles after the signal candle (same timeframe).
-- Reference price = signal candle close (when the Pine alert fires).
-- Percentages are in the signal's direction: favourable > 0 is good, adverse > 0 is bad,
-- close_change_pct > 0 means price moved the signal's way by the Nth close.
-- Only stored once all N candles exist and are contiguous.
CREATE TABLE IF NOT EXISTS outcomes (
    symbol               TEXT    NOT NULL,
    timeframe            TEXT    NOT NULL,
    open_time            INTEGER NOT NULL,
    signal               TEXT    NOT NULL,
    horizon              INTEGER NOT NULL,
    entry_price          REAL    NOT NULL,
    next_open            REAL    NOT NULL,
    max_favourable_pct   REAL    NOT NULL,
    max_adverse_pct      REAL    NOT NULL,
    close_change_pct     REAL    NOT NULL,
    PRIMARY KEY (symbol, timeframe, open_time, signal, horizon)
);

-- Binance funding settlements (every 8h). Positive rate = longs pay shorts, as a fraction of position size.
CREATE TABLE IF NOT EXISTS funding (
    symbol        TEXT    NOT NULL,
    funding_time  INTEGER NOT NULL,
    rate          REAL    NOT NULL,
    PRIMARY KEY (symbol, funding_time)
);

-- Support/resistance levels. known_at = the moment the level could first be known (no hindsight):
-- for a swing, the close of the 3rd candle after it; for the owner's own levels, when they were added.
-- first_logged_at = when the engine first stored the row (wall clock), the proof of forward logging.
-- EMA levels move every candle and are not stored here.
CREATE TABLE IF NOT EXISTS levels (
    symbol           TEXT    NOT NULL,
    level_id         TEXT    NOT NULL,
    source           TEXT    NOT NULL,     -- swing_4h / swing_1d / swing_1w / manual
    kind             TEXT    NOT NULL,     -- H (swing high) / L (swing low) / M (manual)
    price            REAL    NOT NULL,
    formed_at        INTEGER NOT NULL,     -- open time of the swing candle (manual: = known_at)
    known_at         INTEGER NOT NULL,
    expires_at       INTEGER NOT NULL,
    note             TEXT,
    first_logged_at  INTEGER NOT NULL,
    code_version     TEXT,                 -- git commit of the engine that first stored the row
    PRIMARY KEY (symbol, level_id)
);

-- Every approach of price to a level, judged on 4h closes.
-- side: support (price came from above) / resistance (from below).
-- result: held / broke, NULL while the 6-candle window is still open.
-- fakeout: for a break, 1 if price is back on the old side 6 candles later.
-- away_N: price move from the open after the visit was decided to the Nth close, signed so that
--         positive = away from the level on the approach side (a bounce). A break's continuation is negative.
-- complete = 1 once every field is known; complete rows are never rewritten.
-- code_version = git commit of the engine that last wrote the row (the one that completed it, once complete).
CREATE TABLE IF NOT EXISTS level_visits (
    symbol             TEXT    NOT NULL,
    level_id           TEXT    NOT NULL,
    touch_time         INTEGER NOT NULL,   -- open time of the 4h candle that first touched
    source             TEXT    NOT NULL,   -- swing_4h/1d/1w / manual / ema50/ema200 _4h/_1d/_1w
    level_price        REAL    NOT NULL,
    side               TEXT    NOT NULL,
    tests              INTEGER NOT NULL,
    volume_ratio       REAL,               -- touch candle volume / its 20-candle average
    buy_share          REAL,               -- touch candle taker-buy volume / volume
    rsi14              REAL,
    trend_4h           TEXT,
    trend_1d           TEXT,
    funding_rate       REAL,               -- latest settled rate at the touch candle's close
    result             TEXT,
    decided_time       INTEGER,
    break_volume_ratio REAL,
    break_buy_share    REAL,
    fakeout            INTEGER,
    away_6             REAL,
    away_12            REAL,
    away_24            REAL,
    complete           INTEGER NOT NULL DEFAULT 0,
    first_logged_at    INTEGER NOT NULL,
    updated_at         INTEGER NOT NULL,
    code_version       TEXT,
    PRIMARY KEY (symbol, level_id, touch_time)
);
CREATE INDEX IF NOT EXISTS level_visits_by_time ON level_visits (symbol, touch_time);
"""

CANDLE_COLUMNS = ('open_time', 'close_time', 'open', 'high', 'low', 'close', 'volume',
                  'quote_volume', 'trades', 'taker_buy_volume', 'taker_buy_quote_volume')
INDICATOR_COLUMNS = ('open_time', 'ema50', 'ema200', 'rsi14', 'rsi_avg_gain', 'rsi_avg_loss',
                     'volume_sma20', 'bars_seen')


def connect(path) -> sqlite3.Connection:
    path = Path(path)
    if str(path) != ':memory:':
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript(SCHEMA)
    for table, column in (('levels', 'code_version'), ('level_visits', 'code_version')):   # added in schema 4
        if column not in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
    conn.commit()
    return conn


def insert_candles(conn, symbol: str, timeframe: str, candles: list) -> int:
    """Closed candles never change, so existing rows are kept as they are."""
    cols = ('symbol', 'timeframe') + CANDLE_COLUMNS
    sql = f"INSERT OR IGNORE INTO candles ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})"
    before = conn.total_changes
    conn.executemany(sql, [(symbol, timeframe) + tuple(c[k] for k in CANDLE_COLUMNS) for c in candles])
    return conn.total_changes - before


def last_open_time(conn, table: str, symbol: str, timeframe: str):
    row = conn.execute(f"SELECT MAX(open_time) FROM {table} WHERE symbol=? AND timeframe=?",
                       (symbol, timeframe)).fetchone()
    return row[0]


def candles_after(conn, symbol: str, timeframe: str, after_open_time=None) -> list:
    sql = "SELECT * FROM candles WHERE symbol=? AND timeframe=?"
    params = [symbol, timeframe]
    if after_open_time is not None:
        sql += " AND open_time > ?"
        params.append(after_open_time)
    return [dict(r) for r in conn.execute(sql + " ORDER BY open_time", params)]
