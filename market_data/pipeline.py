"""
Recorder pipeline: download closed candles, then derive indicators, Strategy-1
signals, higher-timeframe context and forward outcomes.

Every step only adds what is missing, so `sync` can run every 5 minutes and a
re-run is harmless. `rebuild=True` recomputes all derived tables from the
stored candles.
"""
import logging
import time

from market_data import binance_futures, settings, store, strategy1
from market_data.indicators import IndicatorEngine, trend

log = logging.getLogger(__name__)

DERIVED_TABLES = ('outcomes', 'signal_context', 'signals', 'indicators')


def fetch_candles(conn, timeframes, now_ms=None, get_json=None) -> dict:
    """Download every closed candle after the last stored one (from the first listed candle on a fresh DB)."""
    kwargs = {'get_json': get_json} if get_json else {}
    added = {}
    for tf in timeframes:
        last = store.last_open_time(conn, 'candles', settings.SYMBOL, tf)
        start = last + settings.TIMEFRAMES[tf] if last is not None else 0
        added[tf] = 0
        for page in binance_futures.fetch_closed_klines(tf, start, now_ms=now_ms, **kwargs):
            added[tf] += store.insert_candles(conn, settings.SYMBOL, tf, page)
            conn.commit()
        log.info('%s: %d new candles', tf, added[tf])
    return added


def fetch_funding(conn, get_json=None) -> int:
    """Download funding settlements after the last stored one."""
    kwargs = {'get_json': get_json} if get_json else {}
    row = conn.execute("SELECT MAX(funding_time) FROM funding WHERE symbol=?", (settings.SYMBOL,)).fetchone()
    start = row[0] + 1 if row[0] is not None else settings.FUTURES_LISTING_MS
    added = 0
    for page in binance_futures.fetch_funding(start, **kwargs):
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO funding (symbol, funding_time, rate) VALUES (?, ?, ?)",
                         [(settings.SYMBOL, t, r) for t, r in page])
        added += conn.total_changes - before
        conn.commit()
    log.info('funding: %d new settlements', added)
    return added


def _last_indicator_row(conn, tf):
    row = conn.execute(
        "SELECT * FROM indicators WHERE symbol=? AND timeframe=? ORDER BY open_time DESC LIMIT 1",
        (settings.SYMBOL, tf)).fetchone()
    return dict(row) if row else None


def _clear_timeframe(conn, tf):
    for table in DERIVED_TABLES:
        conn.execute(f"DELETE FROM {table} WHERE symbol=? AND timeframe=?", (settings.SYMBOL, tf))


def update_indicators_and_signals(conn, tf) -> int:
    """Compute indicators and Strategy-1 signals for candles that have none yet. Returns new signal count."""
    last = _last_indicator_row(conn, tf)
    if IndicatorEngine.can_resume(last):
        recent = conn.execute(
            "SELECT close, volume FROM candles WHERE symbol=? AND timeframe=? AND open_time<=? "
            "ORDER BY open_time DESC LIMIT ?",
            (settings.SYMBOL, tf, last['open_time'], settings.VOLUME_SMA_LENGTH)).fetchall()
        engine = IndicatorEngine.resume(last, recent[0]['close'], [r['volume'] for r in reversed(recent)])
        candles = store.candles_after(conn, settings.SYMBOL, tf, last['open_time'])
    else:
        # Not warmed up yet (or empty): start again from the first candle.
        _clear_timeframe(conn, tf)
        engine = IndicatorEngine()
        candles = store.candles_after(conn, settings.SYMBOL, tf)

    ind_rows, sig_rows = [], []
    for c in candles:
        ind = engine.step(c)
        ind_rows.append((settings.SYMBOL, tf) + tuple(ind[k] for k in store.INDICATOR_COLUMNS))
        for sig in strategy1.signals_for(c, ind['ema50'], ind['ema200']):
            ema = ind['ema50'] if sig.endswith('50') else ind['ema200']
            sig_rows.append((settings.SYMBOL, tf, c['open_time'], sig, strategy1.direction(sig), c['close'], ema))

    cols = ('symbol', 'timeframe') + store.INDICATOR_COLUMNS
    conn.executemany(
        f"INSERT OR REPLACE INTO indicators ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", ind_rows)
    conn.executemany(
        "INSERT OR REPLACE INTO signals (symbol, timeframe, open_time, signal, direction, close, ema) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)", sig_rows)
    conn.commit()
    log.info('%s: %d indicator rows, %d signals', tf, len(ind_rows), len(sig_rows))
    return len(sig_rows)


def latest_closed(conn, context_tf: str, at_close_time: int):
    """Latest candle of context_tf whose close_time <= at_close_time, with its indicators."""
    max_open = at_close_time - settings.TIMEFRAMES[context_tf] + 1
    row = conn.execute(
        "SELECT c.open_time, c.close_time, c.close, i.ema50, i.ema200, i.rsi14 "
        "FROM candles c LEFT JOIN indicators i "
        "  ON i.symbol=c.symbol AND i.timeframe=c.timeframe AND i.open_time=c.open_time "
        "WHERE c.symbol=? AND c.timeframe=? AND c.open_time<=? ORDER BY c.open_time DESC LIMIT 1",
        (settings.SYMBOL, context_tf, max_open)).fetchone()
    return dict(row) if row else None


def update_context(conn) -> int:
    """Attach every other timeframe's latest closed candle to signals that have no context yet."""
    pending = conn.execute(
        "SELECT s.timeframe, s.open_time, s.signal FROM signals s WHERE s.symbol=? AND NOT EXISTS ("
        "  SELECT 1 FROM signal_context x WHERE x.symbol=s.symbol AND x.timeframe=s.timeframe"
        "  AND x.open_time=s.open_time AND x.signal=s.signal)", (settings.SYMBOL,)).fetchall()
    rows = []
    for p in pending:
        signal_close_time = p['open_time'] + settings.TIMEFRAMES[p['timeframe']] - 1
        for ctx_tf in settings.TIMEFRAMES:
            if ctx_tf == p['timeframe']:
                continue
            ctx = latest_closed(conn, ctx_tf, signal_close_time)
            if ctx is None:
                continue
            rows.append((settings.SYMBOL, p['timeframe'], p['open_time'], p['signal'], ctx_tf,
                         ctx['open_time'], ctx['close'], ctx['ema50'], ctx['ema200'], ctx['rsi14'],
                         trend(ctx['ema50'], ctx['ema200'])))
    conn.executemany(
        "INSERT OR REPLACE INTO signal_context (symbol, timeframe, open_time, signal, context_tf, ctx_open_time, "
        "ctx_close, ctx_ema50, ctx_ema200, ctx_rsi14, ctx_trend) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    log.info('context rows added: %d for %d signals', len(rows), len(pending))
    return len(rows)


def measure_outcome(direction: str, entry: float, window: list) -> dict:
    """Move after the signal over `window` (the next N candles), in the signal's direction, in percent."""
    hi = max(c['high'] for c in window)
    lo = min(c['low'] for c in window)
    last_close = window[-1]['close']
    if direction == 'LONG':
        fav, adv, chg = hi - entry, entry - lo, last_close - entry
    else:
        fav, adv, chg = entry - lo, hi - entry, entry - last_close
    return {
        'next_open': window[0]['open'],
        'max_favourable_pct': fav / entry * 100,
        'max_adverse_pct': adv / entry * 100,
        'close_change_pct': chg / entry * 100,
    }


def update_outcomes(conn, tf) -> int:
    """Store outcomes for every signal/horizon whose N following candles now exist without gaps."""
    horizons = settings.OUTCOME_HORIZONS
    pending = conn.execute(
        "SELECT s.open_time, s.signal, s.direction, s.close FROM signals s WHERE s.symbol=? AND s.timeframe=? "
        "AND (SELECT COUNT(*) FROM outcomes o WHERE o.symbol=s.symbol AND o.timeframe=s.timeframe "
        "     AND o.open_time=s.open_time AND o.signal=s.signal) < ?",
        (settings.SYMBOL, tf, len(horizons))).fetchall()
    if not pending:
        return 0
    # only the candles from the oldest pending signal on: loading the full 5m history every cycle took ~13s
    candles = store.candles_after(conn, settings.SYMBOL, tf, min(p['open_time'] for p in pending) - 1)
    index = {c['open_time']: i for i, c in enumerate(candles)}
    dur = settings.TIMEFRAMES[tf]
    rows = []
    for p in pending:
        i = index[p['open_time']]
        for h in horizons:
            if i + h >= len(candles) or candles[i + h]['open_time'] - p['open_time'] != h * dur:
                continue                       # not enough candles yet, or a gap in Binance's data
            out = measure_outcome(p['direction'], p['close'], candles[i + 1:i + h + 1])
            rows.append((settings.SYMBOL, tf, p['open_time'], p['signal'], h, p['close'], out['next_open'],
                         out['max_favourable_pct'], out['max_adverse_pct'], out['close_change_pct']))
    conn.executemany(
        "INSERT OR IGNORE INTO outcomes (symbol, timeframe, open_time, signal, horizon, entry_price, next_open, "
        "max_favourable_pct, max_adverse_pct, close_change_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    log.info('%s: %d outcome rows added', tf, len(rows))
    return len(rows)


def sync(conn, timeframes=None, rebuild=False, now_ms=None, get_json=None, get_funding_json=None) -> dict:
    """Fetch new closed candles for every timeframe, then derive everything that is missing."""
    timeframes = list(timeframes or settings.TIMEFRAMES)
    started = time.time()
    added = fetch_candles(conn, timeframes, now_ms=now_ms, get_json=get_json)
    funding = fetch_funding(conn, get_json=get_funding_json)
    if rebuild:
        for table in DERIVED_TABLES:
            conn.execute(f"DELETE FROM {table} WHERE symbol=?", (settings.SYMBOL,))
        conn.commit()
    # Indicators for all timeframes first: context for a signal reads other timeframes' indicators.
    signals = {tf: update_indicators_and_signals(conn, tf) for tf in settings.TIMEFRAMES}
    context = update_context(conn)
    outcomes = {tf: update_outcomes(conn, tf) for tf in settings.TIMEFRAMES}
    return {'candles_added': added, 'funding_added': funding, 'signals_added': signals, 'context_rows_added': context,
            'outcome_rows_added': outcomes, 'seconds': round(time.time() - started, 1)}


def find_gaps(conn, tf, limit=10) -> tuple:
    """Missing candles in the stored history (Binance itself has a few maintenance gaps)."""
    dur = settings.TIMEFRAMES[tf]
    gaps = conn.execute(
        "SELECT prev_open, open_time FROM ("
        "  SELECT open_time, LAG(open_time) OVER (ORDER BY open_time) AS prev_open"
        "  FROM candles WHERE symbol=? AND timeframe=?) WHERE open_time - prev_open != ?",
        (settings.SYMBOL, tf, dur)).fetchall()
    missing = sum((g['open_time'] - g['prev_open']) // dur - 1 for g in gaps)
    return missing, [(g['prev_open'], g['open_time']) for g in gaps[:limit]]
