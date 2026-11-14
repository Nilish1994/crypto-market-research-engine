"""
Plain-text report: how Strategy-1 signals behaved afterwards, per timeframe.

This describes price movement after each signal. It is not trade P&L: the Pine
script defines no stop, target or exit, and no fees are applied.
"""
import statistics

from market_data import settings, strategy1

TF_ORDER = list(settings.TIMEFRAMES)


def _higher(tf):
    return TF_ORDER[TF_ORDER.index(tf) + 1:]


def _stats(rows):
    if not rows:
        return None
    chg = [r['close_change_pct'] for r in rows]
    return {
        'n': len(rows),
        'pct_moved_their_way': 100 * sum(1 for x in chg if x > 0) / len(rows),
        'avg_close_change': statistics.fmean(chg),
        'median_favourable': statistics.median(r['max_favourable_pct'] for r in rows),
        'median_adverse': statistics.median(r['max_adverse_pct'] for r in rows),
    }


def _baseline(conn, tf, horizon, since_open_time):
    """Average N-candle move over ALL candles since the first signal: what 'no strategy' looks like."""
    closes = [r[0] for r in conn.execute(
        "SELECT close FROM candles WHERE symbol=? AND timeframe=? AND open_time>=? ORDER BY open_time",
        (settings.SYMBOL, tf, since_open_time))]
    changes = [(closes[i + horizon] - closes[i]) / closes[i] * 100 for i in range(len(closes) - horizon)]
    if not changes:
        return None
    return {'n': len(changes), 'pct_up': 100 * sum(1 for x in changes if x > 0) / len(changes),
            'avg_change': statistics.fmean(changes)}


def _load(conn, tf, horizon):
    rows = conn.execute(
        "SELECT s.open_time, s.signal, s.direction, o.max_favourable_pct, o.max_adverse_pct, o.close_change_pct "
        "FROM signals s JOIN outcomes o ON o.symbol=s.symbol AND o.timeframe=s.timeframe "
        "  AND o.open_time=s.open_time AND o.signal=s.signal AND o.horizon=? "
        "WHERE s.symbol=? AND s.timeframe=? ORDER BY s.open_time",
        (horizon, settings.SYMBOL, tf)).fetchall()
    ctx = {}
    for r in conn.execute(
            "SELECT open_time, signal, context_tf, ctx_trend FROM signal_context WHERE symbol=? AND timeframe=?",
            (settings.SYMBOL, tf)):
        ctx.setdefault((r['open_time'], r['signal']), {})[r['context_tf']] = r['ctx_trend']
    return [dict(r) | {'ctx': ctx.get((r['open_time'], r['signal']), {})} for r in rows]


def _agrees(row, tfs):
    want = 'UP' if row['direction'] == 'LONG' else 'DOWN'
    return all(row['ctx'].get(t) == want for t in tfs)


def _fmt(label, s):
    if s is None:
        return f"| {label:<34} |      0 |        |         |        |        |"
    return (f"| {label:<34} | {s['n']:>6} | {s['pct_moved_their_way']:>5.1f}% | {s['avg_close_change']:>+6.3f}% "
            f"| {s['median_favourable']:>5.2f}% | {s['median_adverse']:>5.2f}% |")


def build_report(conn, horizon=12) -> str:
    out = [f"# Strategy-1 signals — {settings.SYMBOL} {settings.MARKET}, move over the next {horizon} candles",
           "",
           "Moved their way = price closed in the signal's direction after N candles. Favourable / adverse =",
           "furthest move for / against the signal within those candles (median). No stop, target or fees:",
           "this is price behaviour after the signal, not trade results.",
           ""]
    for tf in TF_ORDER:
        rows = _load(conn, tf, horizon)
        out.append(f"## {tf}")
        if not rows:
            out += ["No signals with a complete outcome yet.", ""]
            continue
        higher = _higher(tf)
        out += ["| Group                              |      n | Their way | Avg close | Fav    | Adv    |",
                "|------------------------------------|-------:|-------:|---------:|-------:|-------:|"]
        for sig in strategy1.LONG_SIGNALS + strategy1.SHORT_SIGNALS:
            group = [r for r in rows if r['signal'] == sig]
            out.append(_fmt(f"{sig} all", _stats(group)))
            if higher:
                out.append(_fmt(f"{sig} + {higher[0]} trend agrees", _stats([r for r in group if _agrees(r, higher[:1])])))
                out.append(_fmt(f"{sig} + all higher TFs agree", _stats([r for r in group if _agrees(r, higher)])))
        base = _baseline(conn, tf, horizon, rows[0]['open_time'])
        if base:
            out.append("")
            out.append(f"Baseline (every {tf} candle since the first signal, n={base['n']}): price was higher after "
                       f"{horizon} candles {base['pct_up']:.1f}% of the time, average change {base['avg_change']:+.3f}%. "
                       "A long signal needs to beat this, a short signal needs to beat its mirror.")
        out.append("")
    return "\n".join(out)
