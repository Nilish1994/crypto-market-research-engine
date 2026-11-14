"""
Daily summary and weekly scorecard written by the research engine (Markdown files, all times UTC).

  data/reports/daily/YYYY-MM-DD.md     one UTC day: trend, RSI, funding, levels near price, visits
  data/reports/weekly/YYYY-Www.md      does each kind of level hold more than random price lines,
                                       and do volume, repeated touches, RSI and trend tell breaks from holds?
Each report is written once, at the first cycle after it is due (from 00:05 UTC; weeks start Monday).

Forward evidence = visits that started after the engine started (meta 'engine_start'). Everything earlier
was computed after the fact on data already explored, so it is shown as context only.
Level sources added later count as forward only from their own start (settings.SOURCE_FORWARD_START).
These are descriptions of price behaviour, not trade signals, and no costs are applied.
"""
import random
import statistics
from datetime import datetime, timedelta, timezone

from market_data import levels, pipeline, settings
from market_data.indicators import trend

DAY_MS = levels.DAY_MS
MIN_N = 30                                   # groups smaller than this are flagged as too few
CHART_NOTE = 'Times are UTC candle opens; your TradingView chart shows UTC+10:30.'


def fmt(ms, with_time=True):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M' if with_time else '%Y-%m-%d')


def pct(x, digits=2):
    return '—' if x is None else f"{x * 100:+.{digits}f}%"


def engine_start(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='engine_start'").fetchone()
    return int(row[0]) if row else None


def wilson(k, n, z=1.645):
    """90% interval for a rate."""
    if not n:
        return None, None
    p = k / n
    d = 1 + z * z / n
    mid = (p + z * z / (2 * n)) / d
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / d
    return mid - half, mid + half


def rate_cell(k, n):
    if not n:
        return '0'
    lo, hi = wilson(k, n)
    flag = ' (too few)' if n < MIN_N else ''
    return f"{k}/{n} = {k / n * 100:.0f}% [{lo * 100:.0f}–{hi * 100:.0f}%]{flag}"


# ── Daily summary ──

def _daily_symbol(conn, day_start, day_end):
    lines = []
    d = conn.execute("SELECT open_time, close FROM candles WHERE symbol=? AND timeframe='1d' AND open_time<? "
                     "ORDER BY open_time DESC LIMIT 8", (settings.SYMBOL, day_end - DAY_MS + 1)).fetchall()
    if not d:
        return [f"## {settings.SYMBOL}", '', 'No daily candles stored yet.', '']
    last = d[0]['close']
    chg1 = last / d[1]['close'] - 1 if len(d) > 1 else None
    chg7 = last / d[7]['close'] - 1 if len(d) > 7 else None
    lines += [f"## {settings.SYMBOL}", '', f"Daily close **{last:,.2f}** ({fmt(d[0]['open_time'], False)}): "
              f"1 day {pct(chg1)}, 7 days {pct(chg7)}", '']

    lines += ['| Timeframe | Close | vs EMA50 | vs EMA200 | Trend (EMA50 vs EMA200) | RSI 14 |', '|---|---|---|---|---|---|']
    for tf in ('1h', '4h', '1d', '1w'):
        r = pipeline.latest_closed(conn, tf, day_end - 1)
        if not r:
            continue
        vs = lambda e: pct(r['close'] / e - 1) if e else '—'
        lines.append(f"| {tf} | {r['close']:,.2f} | {vs(r['ema50'])} | {vs(r['ema200'])} | "
                     f"{trend(r['ema50'], r['ema200']) or '—'} | {r['rsi14']:.1f} |" if r['rsi14'] is not None else
                     f"| {tf} | {r['close']:,.2f} | {vs(r['ema50'])} | {vs(r['ema200'])} | — | — |")
    fr = conn.execute("SELECT funding_time, rate FROM funding WHERE symbol=? AND funding_time<? ORDER BY funding_time DESC "
                      "LIMIT 21", (settings.SYMBOL, day_end)).fetchall()
    if fr:
        lines += ['', f"Funding: last {fr[0]['rate'] * 100:+.4f}% per 8h, 7-day average "
                      f"{statistics.fmean(r['rate'] for r in fr) * 100:+.4f}% (positive = longs pay shorts)"]

    groups = levels.group_nearby(levels.active_levels(conn, day_end))
    above = [g for g in groups if g[0]['price'] > last][:4]
    below = [g for g in groups if g[0]['price'] <= last][-4:]
    lines += ['', '**Levels nearest to price** (visits in the last 90 days: held / broke)', '',
              '| Level | Source | Distance | Visits 90d | Last visit |', '|---|---|---|---|---|']
    for g in list(reversed(above)) + list(reversed(below)):
        ids = [x['level_id'] for x in g]
        marks = ','.join('?' * len(ids))
        v = conn.execute(f"SELECT COUNT(DISTINCT touch_time), SUM(result='held'), SUM(result='broke') FROM ("
                         f"SELECT touch_time, MAX(result) AS result FROM level_visits WHERE symbol=? AND level_id IN ({marks}) "
                         f"AND touch_time>=? GROUP BY touch_time)", (settings.SYMBOL, *ids, day_end - 90 * DAY_MS)).fetchone()
        lastv = conn.execute(f"SELECT touch_time, side, result FROM level_visits WHERE symbol=? AND level_id IN ({marks}) "
                             f"ORDER BY touch_time DESC LIMIT 1", (settings.SYMBOL, *ids)).fetchone()
        last_txt = f"{fmt(lastv['touch_time'])} {lastv['side']} {lastv['result'] or 'open'}" if lastv else '—'
        name = ' + '.join(x['source'] + (f" ({x['note']})" if x.get('note') else '') for x in g)
        lines.append(f"| {g[0]['price']:,.2f} | {name} | {pct(g[0]['price'] / last - 1)} | {v[0]}: {v[1] or 0} / {v[2] or 0} | {last_txt} |")

    rows = conn.execute("SELECT * FROM level_visits WHERE symbol=? AND ((touch_time>=? AND touch_time<?) OR "
                        "(decided_time>=? AND decided_time<?)) ORDER BY touch_time",
                        (settings.SYMBOL, day_start, day_end, day_start, day_end)).fetchall()
    lines += ['', f"**Level visits touched or decided this day: {len(rows)}**", '']
    if rows:
        lines += ['| Touch | Level | Source | Side | Tests | Volume | RSI | Result |', '|---|---|---|---|---|---|---|---|']
        for r in rows:
            res = r['result'] or 'open'
            if r['result'] == 'broke':
                res += ' (fake-out)' if r['fakeout'] == 1 else (' (held beyond)' if r['fakeout'] == 0 else '')
            vol = f"{r['volume_ratio']:.1f}x" if r['volume_ratio'] is not None else '—'
            rsi = f"{r['rsi14']:.0f}" if r['rsi14'] is not None else '—'
            lines.append(f"| {fmt(r['touch_time'])} | {r['level_price']:,.2f} | {r['source']} | {r['side']} | {r['tests']} | "
                         f"{vol} | {rsi} | {res} |")

    lag = []
    for tf, dur in settings.TIMEFRAMES.items():
        if tf == '1w':
            continue
        t = conn.execute("SELECT MAX(open_time) FROM candles WHERE symbol=? AND timeframe=? AND open_time<?",
                         (settings.SYMBOL, tf, day_end)).fetchone()[0]
        if t is None or t + dur < day_end:                  # the day's last candle of this timeframe is missing
            lag.append(f"{tf} last {fmt(t) if t else 'none'}")
    status = conn.execute("SELECT value FROM meta WHERE key=?", (f'sync_status:{settings.SYMBOL}',)).fetchone()
    lines += ['', ('Data: complete for the day.' if not lag else 'Data: **incomplete** — ' + ', '.join(lag))
              + (f" Last download: {status[0]}." if status else ''), '']
    return lines


def daily_report(conn, day_start, symbols=None) -> str:
    day_end = day_start + DAY_MS
    start = engine_start(conn)
    out = [f"# Daily market summary — {fmt(day_start, False)} (UTC)", '', CHART_NOTE,
           'Information only, not trade signals.' + (f" Forward evidence since {fmt(start)} UTC." if start else ''), '']
    keep = settings.SYMBOL
    try:
        for sym in symbols or settings.WATCH_SYMBOLS:
            settings.SYMBOL = sym
            out += _daily_symbol(conn, day_start, day_end)
    finally:
        settings.SYMBOL = keep
    return '\n'.join(out)


# ── Weekly scorecard ──

def _window_candles(conn, lo, hi):
    """(candles, a, b) per symbol, c[a:b] = the 4h candles opening in [lo, hi); sets settings.SYMBOL (callers restore it)."""
    for sym in sorted({r[0] for r in conn.execute("SELECT DISTINCT symbol FROM level_visits")}):
        settings.SYMBOL = sym
        c = levels.load_candles(conn, settings.VISIT_TF, since=lo - 30 * DAY_MS)
        times = [x['open_time'] for x in c]
        a = next((i for i, t in enumerate(times) if t >= lo), len(c))
        b = next((i for i, t in enumerate(times) if t >= hi), len(c))
        if b - a >= 2:
            yield c, a, b


def _flat_line_visits(c, a, b, rnd, n):
    """Decided visits to n random flat lines drawn across the price range of c[a:b]."""
    p_lo, p_hi = min(x['low'] for x in c[a:b]), max(x['high'] for x in c[a:b])
    for _ in range(n):
        p = rnd.uniform(p_lo, p_hi)
        for v in levels.compute_visits(c, lambda i, p=p: p, a, b):
            if v['result'] is not None:
                yield v


def _random_held(conn, lo, hi, lines_per_symbol, seed):
    """
    Visits to random price lines, scored exactly like real levels: held count, total, the 12-candle move
    after holds, and (break-candle volume ratio, fake-out) for every judged break, for static lines;
    plus held count and total for EMA-shaped moving lines (a 4h EMA50/EMA200 shifted 0.5–8% up or down),
    the fair comparison for EMA levels, which move with price.
    """
    rnd = random.Random(seed)
    held = total = m_held = m_total = 0
    away, breaks = [], []
    keep = settings.SYMBOL
    try:
        for c, a, b in _window_candles(conn, lo, hi):
            for v in _flat_line_visits(c, a, b, rnd, lines_per_symbol):
                total += 1
                held += v['result'] == 'held'
                if v['result'] == 'held' and 12 in v['away']:
                    away.append(v['away'][12])
                brk = c[v['break_i']] if v['break_i'] is not None else None
                if brk and v['fakeout'] is not None and brk['volume_sma20']:
                    breaks.append((brk['volume'] / brk['volume_sma20'], v['fakeout']))
            shapes = [[None] + [x[name] for x in c[:-1]] for name in ('ema50', 'ema200')]   # known at each open
            for _ in range(lines_per_symbol):
                shape = rnd.choice(shapes)
                u = rnd.choice((-1, 1)) * rnd.uniform(0.005, 0.08)
                for v in levels.compute_visits(c, lambda i, s=shape, u=u: s[i] * (1 + u) if s[i] else None, a, b):
                    if v['result'] is not None:
                        m_total += 1
                        m_held += v['result'] == 'held'
    finally:
        settings.SYMBOL = keep
    return held, total, away, breaks, m_held, m_total


def _scorecard(conn, lo, hi, title, seed, forward=False):
    # A hold needs 6 more candles to be decided but a break is decided at once, so visits that started in
    # the last 6 candles before `hi` are left for the next report; otherwise breaks would be over-counted.
    hi = hi - settings.VISIT_WINDOW * settings.TIMEFRAMES[settings.VISIT_TF]
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM level_visits WHERE touch_time>=? AND touch_time<? AND result IS NOT NULL", (lo, hi))]
    out = [f"## {title}", '']
    late = {s: t for s, t in settings.SOURCE_FORWARD_START.items() if t > lo} if forward else {}
    if late:
        rows = [r for r in rows if r['touch_time'] >= late.get(r['source'], lo)]
        out += ['Added after the forward start, so counted only from ' + '; '.join(
            f"{', '.join(sorted(s for s in late if late[s] == t))} {fmt(t)} UTC" for t in sorted(set(late.values())))
                + '.', '']
    by_sym = {}
    for r in rows:
        by_sym[r['symbol']] = by_sym.get(r['symbol'], 0) + 1
    out += [f"Decided visits: {len(rows)} (" + ', '.join(f"{s} {n}" for s, n in sorted(by_sym.items())) + ')' if rows
            else 'Decided visits: 0 so far.', '']
    if not rows:
        return out
    versions = sorted({r['code_version'] or 'unstamped' for r in rows})
    out += [f"Written by engine version(s): {', '.join(versions)}", '']

    rh, rt, raway, rbreaks, mh, mt = _random_held(conn, lo, hi, 100, seed)
    out += ['**1. Do levels hold more often than random price lines?**', '',
            '| Level source | Held (90% range) | Avg move after a hold, 12 candles |', '|---|---|---|']
    for src in sorted({r['source'] for r in rows}):
        g = [r for r in rows if r['source'] == src]
        h = [r['away_12'] for r in g if r['result'] == 'held' and r['away_12'] is not None]
        out.append(f"| {src} | {rate_cell(sum(r['result'] == 'held' for r in g), len(g))} | "
                   f"{pct(statistics.fmean(h)) if h else '—'} |")
    out.append(f"| random flat lines (compare swing and manual levels) | {rate_cell(rh, rt)} | "
               f"{pct(statistics.fmean(raway)) if raway else '—'} |")
    out.append(f"| random moving lines, EMA-shaped (compare EMA levels) | {rate_cell(mh, mt)} | — |")

    brk = [r for r in rows if r['result'] == 'broke' and r['fakeout'] is not None and r['break_volume_ratio'] is not None]
    out += ['', '**2. Volume on the breaking candle: are low-volume breaks more often fake-outs?** '
            'Only meaningful where levels differ from random lines: a pattern that random lines show too is '
            'about volume, not about levels.', '',
            '| Break candle volume | Fake-out rate at levels | Fake-out rate at random lines |', '|---|---|---|']
    for label, f in (('1.5x average or more', lambda v: v >= 1.5), ('1.0–1.5x', lambda v: 1.0 <= v < 1.5),
                     ('below average', lambda v: v < 1.0)):
        g = [r for r in brk if f(r['break_volume_ratio'])]
        rg = [fk for vr, fk in rbreaks if f(vr)]
        out.append(f"| {label} | {rate_cell(sum(r['fakeout'] for r in g), len(g))} | {rate_cell(sum(rg), len(rg))} |")

    held = [r for r in rows if r['result'] == 'held' and r['away_12'] is not None]
    out += ['', '**3. Leaning on a level: when price touches the level on 3 or more of the 6 candles of one visit '
            'and it still holds, does price then move away further?**', '',
            '| Held visits | Count | Avg move away, 12 candles | Moved away |', '|---|---|---|---|']
    for label, f in (('touched on 3+ candles', lambda t: t >= 3), ('touched on 1–2 candles', lambda t: t <= 2)):
        g = [r['away_12'] for r in held if f(r['tests'])]
        out.append(f"| {label} | {len(g)}{' (too few)' if len(g) < MIN_N else ''} | "
                   f"{pct(statistics.fmean(g)) if g else '—'} | {sum(x > 0 for x in g)}/{len(g)} |")

    out += ['', '**4. RSI at the touch** (descriptive)', '', '| RSI | Held |', '|---|---|']
    for a, b in ((0, 30), (30, 45), (45, 55), (55, 70), (70, 101)):
        g = [r for r in rows if r['rsi14'] is not None and a <= r['rsi14'] < b]
        out.append(f"| {a}–{min(b, 100)} | {rate_cell(sum(r['result'] == 'held' for r in g), len(g))} |")

    agree = lambda r: (r['side'] == 'support' and r['trend_1d'] == 'UP') or (r['side'] == 'resistance' and r['trend_1d'] == 'DOWN')
    with_t = [r for r in rows if r['trend_1d']]
    out += ['', '**5. Daily trend** (descriptive)', '', '| Level vs daily trend | Held |', '|---|---|',
            f"| support in uptrend / resistance in downtrend | "
            f"{rate_cell(sum(r['result'] == 'held' for r in with_t if agree(r)), sum(agree(r) for r in with_t))} |",
            f"| against the trend | {rate_cell(sum(r['result'] == 'held' for r in with_t if not agree(r)), sum(not agree(r) for r in with_t))} |",
            '']
    return out


def rule_qualifies(rule, side, rsi):
    if rsi is None:
        return False
    return rsi < rule['support_rsi_below'] if side == 'support' else rsi > rule['resistance_rsi_above']


def _random_rule(conn, lo, hi, rule, lines_per_symbol, seed):
    """Held count, total and 12-candle moves after holds for random flat-line visits that meet the rule's RSI condition."""
    rnd = random.Random(seed)
    held = total = 0
    away = []
    keep = settings.SYMBOL
    try:
        for c, a, b in _window_candles(conn, lo, hi):
            for v in _flat_line_visits(c, a, b, rnd, lines_per_symbol):
                if not rule_qualifies(rule, v['side'], c[v['touch_i']]['rsi14']):
                    continue
                total += 1
                held += v['result'] == 'held'
                if v['result'] == 'held' and 12 in v['away']:
                    away.append(v['away'][12])
    finally:
        settings.SYMBOL = keep
    return held, total, away


def rule_verdict(k, n, rk, rn, away, cost, min_visits):
    """The pre-registered pass bar applied to rule held k/n, random-line held rk/rn and the 12-candle moves after holds."""
    if n < min_visits:
        return f"too early: {n}/{min_visits} decided visits."
    (lo_r, hi_r), (lo_b, hi_b) = wilson(k, n), wilson(rk, rn)
    if lo_b is None:
        return 'no random-line comparison available.'
    if lo_r > hi_b and not away:
        return 'pass bar not met yet: held rate above random lines; the move after holds is unavailable.'
    if lo_r > hi_b and statistics.fmean(away) - cost > 0:
        return 'pass bar met: held rate above random lines and positive after cost.'
    if lo_r > hi_b:
        return 'pass bar not met: held rate above random lines, but the move after holds is not positive after cost.'
    if hi_r < lo_b:
        return 'pass bar not met: held rate below random lines.'
    return 'pass bar not met: no demonstrated difference from random lines (ranges overlap).'


def _rule_section(conn, rule, hi, seed):
    """The pre-registered test of one frozen rule: visits after its freeze time against random lines under the same condition."""
    lo = rule['frozen_ms']
    hi = hi - settings.VISIT_WINDOW * settings.TIMEFRAMES[settings.VISIT_TF]   # same hold-back as the scorecard
    cost = settings.ROUND_TRIP_FEE_BPS / 10_000
    marks = ','.join('?' * len(rule['sources']))
    rows = [dict(r) for r in conn.execute(
        f"SELECT symbol, touch_time, side, rsi14, result, away_12 FROM level_visits WHERE source IN ({marks}) "
        "AND touch_time>=? AND touch_time<? AND result IS NOT NULL", (*rule['sources'], lo, hi))]
    rows = [r for r in rows if rule_qualifies(rule, r['side'], r['rsi14'])]
    k, n = sum(r['result'] == 'held' for r in rows), len(rows)
    away = [r['away_12'] for r in rows if r['result'] == 'held' and r['away_12'] is not None]
    rk, rn, raway = _random_rule(conn, lo, hi, rule, 100, seed) if hi > lo else (0, 0, [])

    def net(xs):
        return f"{pct(statistics.fmean(xs) - cost)} ({len(xs)} holds)" if xs else '—'

    out = [f"## Rule {rule['name']} — pre-registered, frozen {fmt(lo)} UTC", '',
           f"Levels: {', '.join(rule['sources'])}. Qualifies: support touched with RSI14 < {rule['support_rsi_below']}, "
           f"or resistance touched with RSI14 > {rule['resistance_rsi_above']}. Success: the level held. Only visits "
           f"after the freeze count. Pass bar: {rule['min_visits']}+ decided visits, a held rate whose 90% range sits "
           f"above random flat lines under the same RSI condition, and a positive average move away from the level "
           f"after holds (12 candles, after the {cost * 100:.2f}% round trip).", '',
           '| | Held (90% range) | Avg move away after a hold, 12 candles, after cost |', '|---|---|---|',
           f"| rule {rule['name']} | {rate_cell(k, n)} | {net(away)} |",
           f"| random flat lines, same RSI condition | {rate_cell(rk, rn)} | {net(raway)} |", '']
    if n:
        touches = len({(r['symbol'], r['touch_time']) for r in rows})
        out += [f"These {n} visits come from {touches} distinct touch candles: visits to nearby levels overlap, so the "
                "ranges are narrower than the evidence really is.", '']
    verdict = rule_verdict(k, n, rk, rn, away, cost, rule['min_visits'])
    out += [f"**Status:** {verdict} Research only, not a trade signal.", '']
    return out


def weekly_report(conn, week_start, symbols=None) -> str:
    week_end = week_start + 7 * DAY_MS
    start = engine_start(conn) or week_end
    iso = datetime.fromtimestamp(week_start / 1000, tz=timezone.utc).isocalendar()
    week_rows = conn.execute("SELECT symbol, COUNT(*), SUM(result='held'), SUM(result='broke') FROM level_visits "
                             "WHERE touch_time>=? AND touch_time<? GROUP BY symbol", (week_start, week_end)).fetchall()
    out = [f"# Weekly scorecard — {iso[0]}-W{iso[1]:02d} ({fmt(week_start, False)} to {fmt(week_end - DAY_MS, False)}, UTC)", '',
           'How the definitions work (fixed in advance): a level is a 4h, daily or weekly swing high/low (known only '
           'after 3 more candles close; weekly swings and EMAs were added on 2026-11-03), an EMA50/EMA200 on 4h, '
           'daily or weekly, or a level you added. A visit starts when a 4h '
           'candle reaches within ±0.15% of the level after 6 closes on one side, with the candle before it clear of '
           'that zone. It **broke** if a close goes beyond the zone within 6 candles, otherwise it **held**. A break '
           'is a **fake-out** if price is back on the old side 6 candles later. Visits from the last 6 candles of the '
           'period wait for the next report, so holds and breaks are counted alike (so visits in the last day before '
           'the forward start appear in neither section). Moves are measured from the next open, before costs (a '
           'round trip costs about 0.10%).', '',
           'Ranges are 90% intervals. Visits to nearby levels overlap, so they are not independent: treat small '
           'differences as noise. Not trade signals.', '',
           '**This week:** ' + (', '.join(f"{r[0]} {r[1]} visits ({r[2] or 0} held, {r[3] or 0} broke, rest open)"
                                          for r in week_rows) or 'no visits.'), '']
    for i, rule in enumerate(settings.FROZEN_RULES):
        if rule['frozen_ms'] < week_end:
            out += _rule_section(conn, rule, week_end, seed=3 + i)
    out += _scorecard(conn, start, week_end, f"Forward evidence (since {fmt(start)} UTC) — the test that counts", seed=1,
                      forward=True)
    out += _scorecard(conn, settings.HISTORY_START_MS, min(start, week_end),
                      f"History (2020 to {fmt(min(start, week_end), False)}) — explored data, context only", seed=2)
    return '\n'.join(out)


# ── Writing files ──

def due_reports(now_ms, report_dir=None) -> list:
    """(kind, start_ms, path) for yesterday's daily and last week's weekly report if not written yet."""
    report_dir = report_dir or settings.REPORT_DIR
    now = datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc)
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now < today + timedelta(minutes=settings.DAILY_REPORT_AFTER_MIN):
        today -= timedelta(days=1)
    out = []
    day = today - timedelta(days=1)
    path = report_dir / 'daily' / f"{day:%Y-%m-%d}.md"
    if not path.exists():
        out.append(('daily', int(day.timestamp() * 1000), path))
    week = today - timedelta(days=today.weekday() + 7)
    iso = week.isocalendar()
    path = report_dir / 'weekly' / f"{iso[0]}-W{iso[1]:02d}.md"
    if not path.exists():
        out.append(('weekly', int(week.timestamp() * 1000), path))
    return out


def write_due_reports(conn, now_ms, symbols=None, report_dir=None, version=None) -> list:
    written = []
    for kind, start, path in due_reports(now_ms, report_dir):
        text = daily_report(conn, start, symbols) if kind == 'daily' else weekly_report(conn, start, symbols)
        text += f"\n---\nWritten {fmt(now_ms)} UTC by engine version {version or 'unknown'}."
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text + '\n')
        written.append(str(path))
    return written
