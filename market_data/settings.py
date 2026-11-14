"""Fixed settings for the market-data recorder."""
import os
from pathlib import Path

SYMBOL = os.getenv('MARKET_DATA_SYMBOL', 'BTCUSDT')   # any Binance USD-M perpetual; CLI: --symbol
MARKET = 'binance_usdm_futures'          # TradingView: BINANCE:<SYMBOL>.P
KLINES_URL = 'https://fapi.binance.com/fapi/v1/klines'
KLINES_LIMIT = 1500                      # Binance maximum per request
FUNDING_URL = 'https://fapi.binance.com/fapi/v1/fundingRate'
FUNDING_LIMIT = 1000
FUTURES_LISTING_MS = 1567296000000       # 2019-09-01, before the first BTCUSDT perpetual candle
REQUEST_PAUSE_SEC = 0.3                  # keeps well under the 2400 weight/min IP limit

MINUTE_MS = 60_000
TIMEFRAMES = {                           # Binance interval -> candle length in ms
    '5m':  5 * MINUTE_MS,
    '15m': 15 * MINUTE_MS,
    '1h':  60 * MINUTE_MS,
    '4h':  240 * MINUTE_MS,
    '1d':  1440 * MINUTE_MS,
    '1w':  10080 * MINUTE_MS,            # Binance weeks start Monday 00:00 UTC, like TradingView
}

EMA_FAST = 50
EMA_SLOW = 200
RSI_LENGTH = 14                          # TradingView default
VOLUME_SMA_LENGTH = 20                   # TradingView default volume MA

RETEST_ZONE_PCT = 0.0015                 # Strategy-1: ±0.15% around each EMA
OUTCOME_HORIZONS = (3, 6, 12)            # candles after the signal, same timeframe
ROUND_TRIP_FEE_BPS = 10                  # fair test default: 0.05% taker per side

DB_PATH = Path(os.getenv('MARKET_DATA_DB', Path(__file__).resolve().parent.parent / 'data' / 'market_data.db'))

# ── Research engine (python -m market_data watch) ──
WATCH_SYMBOLS = tuple(s.strip().upper() for s in os.getenv('MARKET_DATA_WATCH_SYMBOLS', 'BTCUSDT,ETHUSDT,SOLUSDT').split(',') if s.strip())
REPORT_DIR = Path(os.getenv('MARKET_DATA_REPORTS', DB_PATH.parent / 'reports'))
HISTORY_START_MS = 1577836800000         # 2020-01-01: levels and visits are computed from here
SWING_TIMEFRAMES = ('4h', '1d', '1w')    # swing highs/lows on these timeframes become levels
SWING_K = 3                              # a swing needs 3 candles on each side; known only after the 3rd closes
LEVEL_MERGE_PCT = 0.003                  # a new swing within 0.3% of an active level does not make a new level
LEVEL_LIFETIME_DAYS = {'4h': 90, '1d': 365, '1w': 1095, 'manual': 3650}
EMA_LEVELS = (('ema50', '4h'), ('ema200', '4h'), ('ema50', '1d'), ('ema200', '1d'), ('ema50', '1w'), ('ema200', '1w'))
VISIT_TF = '4h'                          # visits are judged on 4h closes
LEVEL_ZONE_PCT = RETEST_ZONE_PCT         # a touch is within ±0.15% of the level
VISIT_SIDE_CANDLES = 6                   # closes on one side before a touch counts as an approach
VISIT_WINDOW = 6                         # touch candle + 5: a close beyond the zone inside it = broke
FAKEOUT_CHECK = 6                        # a break is a fake-out if price is back on the old side 6 candles later
VISIT_HORIZONS = (6, 12, 24)             # move measured after the visit is decided, in 4h candles
DAILY_REPORT_AFTER_MIN = 5               # write yesterday's summary from 00:05 UTC

# Level sources added after the forward start (meta 'engine_start') count as forward evidence only for visits
# that touch from this time; earlier visits to them are computed in one pass at deploy, not logged live.
SOURCE_FORWARD_START = {src: 1793779200000 for src in ('swing_1w', 'ema50_1w', 'ema200_1w')}   # 2026-11-04 08:00 UTC

# Pre-registered rules the weekly scorecard tests on visits after their freeze time. A frozen rule is never
# edited: any change is a new rule with a new name and freeze time, and its count starts again.
FROZEN_RULES = (
    {'name': 'v1b', 'frozen_ms': 1793706420000,          # 2026-11-03 11:47 UTC
     'sources': ('swing_4h', 'swing_1d'),
     'support_rsi_below': 30, 'resistance_rsi_above': 70,  # strict: RSI exactly 30 or 70 does not qualify
     'min_visits': 100},
)
