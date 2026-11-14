"""
Support/resistance levels and every visit price makes to them, recorded without hindsight.

Levels
  swing_4h / _1d / _1w   a candle whose high (low) is strictly above (below) the 3 candles on each side.
                         It can only be known once the 3rd candle after it has closed (known_at).
                         A new swing within 0.3% of a level of the same timeframe that is still active does
                         not make a new level; timeframes are merged separately.
                         Levels expire 90 days (4h) / 365 days (1d) / 1095 days (1w) after they become known.
  manual                 the owner's own levels, known from the moment they are added.
  ema50/ema200 4h/1d/1w  moving levels; a 4h candle uses the value known when it opened
                         (the previous 4h candle's EMA; the last daily or weekly candle that had closed).

Visits (judged on 4h closes)
  approach  6 closes on one side, and the previous candle clear of the ±0.15% zone
  touch     the candle reaches the zone: its high from below (resistance), its low from above (support)
  window    touch candle + 5; a close beyond the far edge of the zone inside it = broke, otherwise held
  fakeout   a break where price closes back on the old side 6 candles later
  away_N    move from the open after the deciding candle to the Nth close; positive = bounce direction
"""
import bisect
import time

from market_data import settings
from market_data.indicators import trend

DAY_MS = 86_400_000

VISIT_COLUMNS = ('symbol', 'level_id', 'touch_time', 'source', 'level_price', 'side', 'tests', 'volume_ratio',
                 'buy_share', 'rsi14', 'trend_4h', 'trend_1d', 'funding_rate', 'result', 'decided_time',
                 'break_volume_ratio', 'break_buy_share', 'fakeout', 'away_6', 'away_12', 'away_24',
                 'complete', 'first_logged_at', 'updated_at', 'code_version')
_NOT_UPDATED = ('symbol', 'level_id', 'touch_time', 'first_logged_at')


def now_ms() -> int:
    return int(time.time() * 1000)


def load_candles(conn, tf, since=None) -> list:
    sql = ("SELECT c.open_time, c.open, c.high, c.low, c.close, c.volume, c.taker_buy_volume, "
           "i.ema50, i.ema200, i.rsi14, i.volume_sma20 FROM candles c LEFT JOIN indicators i "
           "ON i.symbol=c.symbol AND i.timeframe=c.timeframe AND i.open_time=c.open_time "
           "WHERE c.symbol=? AND c.timeframe=?")
    params = [settings.SYMBOL, tf]
    if since is not None:
        sql += " AND c.open_time >= ?"
        params.append(since)
    return [dict(r) for r in conn.execute(sql + " ORDER BY c.open_time", params)]


# ── Levels ──

def find_swings(candles, tf, k=None) -> list:
    """Swing highs/lows with the time each could first be known, merged so nearby swings make one level."""
    k = k or settings.SWING_K
    dur = settings.TIMEFRAMES[tf]
    life = settings.LEVEL_LIFETIME_DAYS[tf] * DAY_MS
    found = []
    for i in range(k, len(candles) - k):
        win = candles[i - k:i + k + 1]
        known = candles[i + k]['open_time'] + dur          # the 3rd candle after the swing has closed
        for kind, key, best in (('H', 'high', max), ('L', 'low', min)):
            px = candles[i][key]
            if px == best(c[key] for c in win) and sum(c[key] == px for c in win) == 1:
                found.append({'level_id': f"swing_{tf}:{candles[i]['open_time']}:{kind}", 'source': f'swing_{tf}',
                              'kind': kind, 'price': px, 'formed_at': candles[i]['open_time'],
                              'known_at': known, 'expires_at': known + life})
    return merge_levels(found)


def merge_levels(levels, pct=None) -> list:
    """Keep a level only if no kept level is active and within pct when it becomes known."""
    pct = settings.LEVEL_MERGE_PCT if pct is None else pct
    kept, active = [], []
    for lv in sorted(levels, key=lambda x: (x['known_at'], x['formed_at'], x['kind'])):
        active = [a for a in active if a['expires_at'] > lv['known_at']]
        if not any(abs(lv['price'] - a['price']) <= pct * a['price'] for a in active):
            kept.append(lv)
            active.append(lv)
    return kept


def update_levels(conn, now=None, code_version=None) -> int:
    """Store newly known swing levels. Existing rows (and their first_logged_at) are never changed."""
    now = now or now_ms()
    added = 0
    for tf in settings.SWING_TIMEFRAMES:
        rows = [(settings.SYMBOL, lv['level_id'], lv['source'], lv['kind'], lv['price'], lv['formed_at'],
                 lv['known_at'], lv['expires_at'], None, now, code_version)
                for lv in find_swings(load_candles(conn, tf, since=settings.HISTORY_START_MS), tf)]
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO levels (symbol, level_id, source, kind, price, formed_at, known_at, "
                         "expires_at, note, first_logged_at, code_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        added += conn.total_changes - before
    conn.commit()
    return added


def add_manual_level(conn, price: float, note=None, at=None, code_version=None) -> str:
    at = at or now_ms()
    level_id = f'manual:{at}'
    conn.execute("INSERT INTO levels (symbol, level_id, source, kind, price, formed_at, known_at, expires_at, note, "
                 "first_logged_at, code_version) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (settings.SYMBOL, level_id, 'manual', 'M', price, at, at,
                  at + settings.LEVEL_LIFETIME_DAYS['manual'] * DAY_MS, note, at, code_version))
    conn.commit()
    return level_id


# ── Visits ──

def compute_visits(c, value, lo=0, hi=None) -> list:
    """Visits that START in c[lo:hi]; value(i) = the level price known when candle i opened (None = inactive)."""
    z, side_n = settings.LEVEL_ZONE_PCT, settings.VISIT_SIDE_CANDLES
    hi = len(c) if hi is None else min(hi, len(c))
    out, i = [], max(lo, side_n + 1)
    while i < hi:
        lv = value(i)
        if lv is None or not (c[i]['low'] <= lv * (1 + z) and c[i]['high'] >= lv * (1 - z)):
            i += 1
            continue
        if c[i - 1]['high'] < lv * (1 - z) and all(c[k]['close'] < lv for k in range(i - side_n, i)):
            side = 'resistance'
        elif c[i - 1]['low'] > lv * (1 + z) and all(c[k]['close'] > lv for k in range(i - side_n, i)):
            side = 'support'
        else:
            i += 1
            continue
        v = judge_visit(c, value, i, side)
        out.append(v)
        i = v['end'] + 1
    return out


def judge_visit(c, value, i, side) -> dict:
    z, win, n = settings.LEVEL_ZONE_PCT, settings.VISIT_WINDOW, len(c)
    up = side == 'resistance'                               # price is pushing up into the level

    def lvl(k):
        v = value(k)
        return v if v is not None else value(i)             # a level expiring mid-window keeps its last price

    def touch(k):
        return c[k]['high'] >= lvl(k) * (1 - z) if up else c[k]['low'] <= lvl(k) * (1 + z)

    def broke(k):
        return c[k]['close'] > lvl(k) * (1 + z) if up else c[k]['close'] < lvl(k) * (1 - z)

    last = min(i + win, n) - 1
    j = next((k for k in range(i, last + 1) if broke(k)), None)
    decided = j is not None or i + win - 1 < n
    end = j if j is not None else last
    v = {'touch_i': i, 'end': end, 'side': side, 'level_price': value(i),
         'tests': sum(touch(k) for k in range(i, end + 1)),
         'result': None, 'decided_i': None, 'break_i': None, 'fakeout': None, 'away': {}}
    if not decided:
        return v
    v['result'] = 'broke' if j is not None else 'held'
    v['decided_i'] = end
    if j is not None:
        v['break_i'] = j
        f = j + settings.FAKEOUT_CHECK
        if f < n:
            v['fakeout'] = int(c[f]['close'] < lvl(f) if up else c[f]['close'] > lvl(f))
    sign = -1 if up else 1                                  # bounce: down from resistance, up from support
    for h in settings.VISIT_HORIZONS:
        if end + h < n:
            v['away'][h] = sign * (c[end + h]['close'] / c[end + 1]['open'] - 1)
    return v


def _ratio(a, b):
    return a / b if a is not None and b else None


def update_visits(conn, now=None, code_version=None) -> dict:
    """
    Recompute visits from stored candles and upsert them. Rows already complete are left untouched;
    unfinished rows that the recompute no longer produces are removed.
    """
    now = now or now_ms()
    c = load_candles(conn, settings.VISIT_TF, since=settings.HISTORY_START_MS - 30 * DAY_MS)
    if len(c) <= settings.VISIT_SIDE_CANDLES + 1:
        return {'visits': 0, 'written': 0}
    times = [x['open_time'] for x in c]
    lo = bisect.bisect_left(times, settings.HISTORY_START_MS)
    dur = settings.TIMEFRAMES[settings.VISIT_TF]

    def closed_by_fn(tf):
        """closed_by(ms) = the last tf candle that had closed by ms."""
        rows = load_candles(conn, tf)
        ts, tf_dur = [x['open_time'] for x in rows], settings.TIMEFRAMES[tf]

        def closed_by(ms):
            k = bisect.bisect_right(ts, ms - tf_dur) - 1
            return rows[k] if k >= 0 else None
        return closed_by

    daily_closed_by = closed_by_fn('1d')

    fund = conn.execute("SELECT funding_time, rate FROM funding WHERE symbol=? ORDER BY funding_time",
                        (settings.SYMBOL,)).fetchall()
    f_times = [r[0] for r in fund]

    def funding_at(ms):
        k = bisect.bisect_right(f_times, ms) - 1
        return fund[k][1] if k >= 0 else None

    series = []                                             # (level_id, source, value_fn, first_i, end_i)
    for r in conn.execute("SELECT level_id, source, price, known_at, expires_at FROM levels WHERE symbol=?",
                          (settings.SYMBOL,)):
        a = max(lo, bisect.bisect_left(times, r['known_at']))
        b = bisect.bisect_left(times, r['expires_at'])
        if a < b:
            series.append((r['level_id'], r['source'], lambda i, a=a, b=b, p=r['price']: p if a <= i < b else None, a, b))
    for name, tf in settings.EMA_LEVELS:
        if tf == settings.VISIT_TF:
            vals = [None] + [x[name] for x in c[:-1]]
        else:
            closed_by = daily_closed_by if tf == '1d' else closed_by_fn(tf)
            vals = [(closed_by(x['open_time']) or {}).get(name) for x in c]
        series.append((f'{name}_{tf}', f'{name}_{tf}', lambda i, v=vals: v[i], lo, len(c)))

    rows = []
    for level_id, source, value, a, b in series:
        for v in compute_visits(c, value, a, b):
            t = c[v['touch_i']]
            close_ms = t['open_time'] + dur
            d = daily_closed_by(close_ms)
            brk = c[v['break_i']] if v['break_i'] is not None else None
            complete = (v['result'] is not None and (v['result'] == 'held' or v['fakeout'] is not None)
                        and len(v['away']) == len(settings.VISIT_HORIZONS))
            rows.append({
                'symbol': settings.SYMBOL, 'level_id': level_id, 'touch_time': t['open_time'], 'source': source,
                'level_price': v['level_price'], 'side': v['side'], 'tests': v['tests'],
                'volume_ratio': _ratio(t['volume'], t['volume_sma20']),
                'buy_share': _ratio(t['taker_buy_volume'], t['volume']),
                'rsi14': t['rsi14'], 'trend_4h': trend(t['ema50'], t['ema200']),
                'trend_1d': trend(d['ema50'], d['ema200']) if d else None,
                'funding_rate': funding_at(close_ms), 'result': v['result'],
                'decided_time': c[v['decided_i']]['open_time'] if v['decided_i'] is not None else None,
                'break_volume_ratio': _ratio(brk['volume'], brk['volume_sma20']) if brk else None,
                'break_buy_share': _ratio(brk['taker_buy_volume'], brk['volume']) if brk else None,
                'fakeout': v['fakeout'], 'away_6': v['away'].get(6), 'away_12': v['away'].get(12),
                'away_24': v['away'].get(24), 'complete': int(complete), 'first_logged_at': now, 'updated_at': now,
                'code_version': code_version})

    updates = ', '.join(f'{k}=excluded.{k}' for k in VISIT_COLUMNS if k not in _NOT_UPDATED)
    sql = (f"INSERT INTO level_visits ({', '.join(VISIT_COLUMNS)}) VALUES ({', '.join('?' * len(VISIT_COLUMNS))}) "
           f"ON CONFLICT(symbol, level_id, touch_time) DO UPDATE SET {updates} WHERE level_visits.complete = 0")
    before = conn.total_changes
    conn.executemany(sql, [tuple(r[k] for k in VISIT_COLUMNS) for r in rows])
    written = conn.total_changes - before
    produced = {(r['level_id'], r['touch_time']) for r in rows}
    stale = [tuple(r) for r in conn.execute("SELECT level_id, touch_time FROM level_visits WHERE symbol=? AND complete=0",
                                            (settings.SYMBOL,)) if (r[0], r[1]) not in produced]
    conn.executemany("DELETE FROM level_visits WHERE symbol=? AND level_id=? AND touch_time=? AND complete=0",
                     [(settings.SYMBOL, *k) for k in stale])
    conn.commit()
    return {'visits': len(rows), 'written': written, 'stale_removed': len(stale)}


def group_nearby(levels_list, pct=0.001) -> list:
    """For display: levels within 0.1% of each other (e.g. the same swing on 4h and daily) shown as one line."""
    groups = []
    for x in sorted(levels_list, key=lambda x: x['price']):
        if groups and x['price'] - groups[-1][0]['price'] <= pct * groups[-1][0]['price']:
            groups[-1].append(x)
        else:
            groups.append([x])
    return groups


def active_levels(conn, at_ms) -> list:
    """Every level known and not expired at at_ms, plus the EMA levels' latest known values."""
    out = [dict(r) for r in conn.execute(
        "SELECT level_id, source, kind, price, known_at, note FROM levels WHERE symbol=? AND known_at<=? AND expires_at>?",
        (settings.SYMBOL, at_ms, at_ms))]
    for name, tf in settings.EMA_LEVELS:
        r = conn.execute("SELECT i.open_time, i." + name + " FROM indicators i WHERE i.symbol=? AND i.timeframe=? "
                         "AND i.open_time<=? AND i." + name + " IS NOT NULL ORDER BY i.open_time DESC LIMIT 1",
                         (settings.SYMBOL, tf, at_ms - settings.TIMEFRAMES[tf])).fetchone()
        if r:
            out.append({'level_id': f'{name}_{tf}', 'source': f'{name}_{tf}', 'kind': 'E', 'price': r[1],
                        'known_at': r[0] + settings.TIMEFRAMES[tf], 'note': None})
    return out
