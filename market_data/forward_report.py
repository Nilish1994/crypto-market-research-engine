"""
Weekly scorecard section for a frozen signal rule (settings.FROZEN_SIGNAL_RULES), written for rule s1h12.

The numbers that decide anything come from forward_rule.evaluate. This file only turns them into text and
adds the descriptive comparisons declared in advance (RULE_RESEARCH.md section 6.7). The descriptions have
NO say in the verdict, and an error in one of them never stops the section from being written.
"""
import logging
import statistics
from datetime import datetime, timezone

from market_data import forward_rule

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
REF_DAYS = 30                 # random-entry comparison: every candle in the 30 days after the signal (harness v3)
MIN_REF = 24                  # a trade with fewer later candles than this is left out of that comparison
VOLUME_SPLIT = 1.5            # signal candle volume / its 20-candle average
BUY_SHARE_SPLIT = 0.5         # signal candle taker-buy volume / volume
MAKER_ROUND_TRIP_BPS = 4      # 2 bps maker fee per side, no slippage. Fees only: NOT a model of getting filled.
MIN_N = 30                    # groups smaller than this are flagged as too few


def fmt(ms, with_time=True):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M' if with_time else '%Y-%m-%d')


def bps(x, digits=1):
    return '—' if x is None else f"{round(x * 10_000, digits) + 0.0:+.{digits}f} bps"     # + 0.0: never '-0.0'


def _mean(xs):
    xs = list(xs)
    return statistics.fmean(xs) if xs else None


def rule_text(rule) -> str:
    return (f"Coins {', '.join(rule['symbols'])}, pooled. Signal: Strategy-1 on a closed {rule['timeframe']} candle "
            f"({'/'.join(rule['long_signals'])} = long, {'/'.join(rule['short_signals'])} = short, zone "
            f"±{rule['zone_pct'] * 100:.2f}%). Entry at the next candle's open, exit at the close of candle "
            f"{rule['hold']} after the signal, no stop and no target, one position per coin. Result per trade: move in "
            f"the trade's direction minus {rule['cost_bps']} bps round trip minus the funding paid. Only signal "
            f"candles that open at or after the start count.")


def status_text(rule, ev) -> str:
    n1, n2 = rule['futility_trades'], rule['target_trades']
    cap = fmt(ev['cap_ms'], False)
    state = ev['state']
    if state == 'void':
        return f"VOID: {ev['reason']}. A void test is restarted under a new name; no verdict is given."
    if state == 'dead_futility':
        return (f"DEAD, stopped at the {n1}-trade check: the first {n1} trades averaged "
                f"{bps(ev['futility']['mean'])} after costs, which is not above zero. Final for this rule; "
                f"no re-run with changed settings under this name.")
    if state in ('alive', 'dead'):
        t = ev['test']
        if t['p'] is None:
            detail = f"{t['n']} trades in {t['weeks']} week(s) are too few to test"
        else:
            detail = (f"{t['n']} trades in {t['weeks']} weeks, mean net {bps(t['mean'])}, standard error "
                      f"{bps(t['se'])[1:]} (clustered by week), t = {t['t']:.2f} on {t['weeks'] - 1} degrees of "
                      f"freedom, one-sided p = {t['p']:.4f} against the bar of {rule['alpha']}")
        if state == 'alive':
            return (f"ALIVE: the single test passed ({detail}). This means worth a deeper look, for example costs "
                    f"with finer fills. It is not a trade signal and execution stays disabled.")
        return f"DEAD: no edge shown ({detail}). Final for this rule; no re-run with changed settings under this name."
    n = len(ev['sample'])
    text = f"no verdict until {n1} / {n2} trades. Counted so far: {n}."
    if ev['futility']:
        text += (f" The {n1}-trade check is done and did not stop the test. Nothing is tested again until trade "
                 f"{n2} or the cap on {cap}, whichever comes first.")
    else:
        text += (f" At {n1} trades the test stops as dead if the average is not above zero; the one test is at "
                 f"{n2} trades or the cap on {cap}, whichever comes first.")
    return text


def _group_row(label, trades):
    if not trades:
        return f"| {label} | 0 | — | — | — |"
    net = [t['net'] for t in trades]
    flag = ' (too few)' if len(net) < MIN_N else ''
    return (f"| {label} | {len(net)}{flag} | {bps(_mean(net))} | {bps(_mean(t['gross'] for t in trades))} | "
            f"{sum(x > 0 for x in net)}/{len(net)} |")


GROUP_HEADER = ['| Group | Trades | Mean net | Mean gross | Won |', '|---|---|---|---|---|']


def _all_entries(rule, mkt, direction, until):
    """{signal time: net} for a trade from EVERY stored candle of the coin that exits by `until`."""
    out = {}
    for t in mkt.candles:
        if t + (rule['hold'] + 1) * mkt.dur > until:
            continue
        status, result = forward_rule.measure(rule, mkt, t, direction)
        if status == 'ok':
            out[t] = result['net']
    return out


def descriptions(conn, rule, ev) -> list:
    sample = ev['sample']
    out = ['**Descriptions (declared in advance; they have no say in the verdict).** Small groups are noise.', '']
    if not sample:
        return out + ['No counted trades yet.', '']
    markets = {s: forward_rule.Market(conn, rule, s) for s in rule['symbols']}

    out += ['By coin and direction:', ''] + GROUP_HEADER
    for s in rule['symbols']:
        out.append(_group_row(s, [t for t in sample if t['symbol'] == s]))
    for d in ('LONG', 'SHORT'):
        out.append(_group_row(d.lower(), [t for t in sample if t['direction'] == d]))

    def volume_ratio(t):
        c = markets[t['symbol']].candles.get(t['signal_time'])
        return c['volume'] / c['volume_sma20'] if c and c['volume_sma20'] else None

    def buy_share(t):
        c = markets[t['symbol']].candles.get(t['signal_time'])
        return c['taker_buy_volume'] / c['volume'] if c and c['volume'] else None

    out += ['', 'By the signal candle\'s volume (against its 20-candle average) and taker-buy share:', ''] + GROUP_HEADER
    for label, f, cut in (('volume', volume_ratio, VOLUME_SPLIT), ('taker-buy share', buy_share, BUY_SHARE_SPLIT)):
        vals = [(f(t), t) for t in sample]
        out.append(_group_row(f"{label} {cut:g} or more", [t for v, t in vals if v is not None and v >= cut]))
        out.append(_group_row(f"{label} below {cut:g}", [t for v, t in vals if v is not None and v < cut]))

    # Random entries in the 30 days after each signal: same coin, direction, exit and costs.
    until = ev['resolved_to']
    entries = {(s, d): _all_entries(rule, markets[s], d, until) for s in rule['symbols'] for d in ('LONG', 'SHORT')}
    excess, refs = [], []
    for t in sample:
        pool, dur = entries[(t['symbol'], t['direction'])], markets[t['symbol']].dur
        window = [pool[u] for u in range(t['signal_time'] + dur, t['signal_time'] + REF_DAYS * DAY_MS + 1, dur) if u in pool]
        if len(window) >= MIN_REF:
            ref = statistics.fmean(window)
            excess.append(t['net'] - ref)
            refs.append(ref)
    out += ['', f"Against random entries (every candle in the {REF_DAYS} days after each signal, same coin, direction, "
                f"exit and costs; trades with fewer than {MIN_REF} later candles are left out, so recent trades use "
                f"a shorter window and this number moves as data arrives):", '']
    if excess:
        out += [f"- trades compared: {len(excess)} of {len(sample)}",
                f"- average random entry: {bps(_mean(refs))}",
                f"- average excess of the rule's trades over them: {bps(_mean(excess))}"]
    else:
        out.append('- not enough later candles yet.')

    # Trend-only control: every candle in the same EMA50/EMA200 trend state inside the test window.
    window_end = sample[-1]['exit_close']
    cells, control_sum, matched = {}, 0.0, []
    for t in sample:
        cells.setdefault((t['symbol'], t['direction']), []).append(t)
    for (s, d), trades in cells.items():
        mkt = markets[s]
        nets = [net for u, net in entries[(s, d)].items()
                if u + (rule['hold'] + 1) * mkt.dur <= window_end and mkt.candles[u]['ema50'] is not None
                and mkt.candles[u]['ema200'] is not None
                and (mkt.candles[u]['ema50'] > mkt.candles[u]['ema200'] if d == 'LONG'
                     else mkt.candles[u]['ema50'] < mkt.candles[u]['ema200'])]
        if nets:
            control_sum += statistics.fmean(nets) * len(trades)
            matched += trades
    out += ['', 'Trend-only control (an entry on every candle in the same trend state, uptrend for longs and downtrend '
                'for shorts, same coin, exit and costs, weighted like the rule\'s trades). It shows whether the '
                'retest candle adds anything beyond the trend:', '']
    if matched:
        control = control_sum / len(matched)
        rule_mean = _mean(t['net'] for t in matched)
        out += [f"- rule's trades: {bps(rule_mean)} ({len(matched)} trades)",
                f"- trend-only entries: {bps(control)}",
                f"- difference: {bps(rule_mean - control)}"]
    else:
        out.append('- not available yet.')

    maker = _mean(t['gross'] - t['funding'] for t in sample) - MAKER_ROUND_TRIP_BPS / 10_000
    out += ['', f"Maker-fee line: with a {MAKER_ROUND_TRIP_BPS} bps round trip (maker fees, no slippage) instead of "
                f"{rule['cost_bps']} bps the mean net would be {bps(maker)}. This changes the fee only. It is not a "
                f"model of getting filled: limit orders that fill are often the ones that go on to lose.", '']
    return out


def section(conn, rule, as_of_ms) -> list:
    """Scorecard lines for one frozen signal rule; nothing before its start. Never raises: the rest of the
    weekly scorecard must still be written if this rule's section cannot be."""
    try:
        return _section(conn, rule, as_of_ms)
    except Exception as e:
        log.exception('section failed for rule %s', rule['name'])
        return [f"## Rule {rule['name']} — pre-registered forward test", '',
                f"**Status:** this section could not be computed this week ({e!r}). No count and no verdict are "
                f"given. Research only, not a trade signal.", '']


def _section(conn, rule, as_of_ms) -> list:
    ev = forward_rule.evaluate(conn, rule, as_of_ms)
    if ev['state'] == 'not_started':
        return []
    sample, n2 = ev['sample'], rule['target_trades']
    n = len(sample)
    out = [f"## Rule {rule['name']} — pre-registered forward test, started {fmt(rule['frozen_ms'])} UTC", '',
           rule_text(rule), '',
           f"**Status:** {status_text(rule, ev)} Research only, not a trade signal.", '']
    net, gross = _mean(t['net'] for t in sample), _mean(t['gross'] for t in sample)
    funding = sum(t['funding'] for t in sample)
    waiting = ev['taken'] - len(ev['counted']) - ev['left_out_gap']
    out += ['| | |', '|---|---|',
            f"| Trades counted | {n} of {n2} ({min(n / n2, 1) * 100:.1f}%) |",
            f"| Mean net per trade | {bps(net)} |",
            f"| Mean gross per trade (before costs and funding) | {bps(gross)} |",
            f"| Fees and slippage | {rule['cost_bps']} bps per trade, {rule['cost_bps'] * n:,} bps in total |",
            f"| Funding paid (negative = received) | {bps(funding / n if n else None, 2)} per trade, "
            f"{bps(funding)} in total |",
            f"| Signals skipped because the coin already had a trade open | {ev['skipped_busy']} |",
            f"| Trades left out because a candle in their window is missing | {ev['left_out_gap']} |",
            f"| Trades taken but not counted yet (still open, or waiting for candles or funding) | {waiting} |",
            f"| 1h candles missing since the start | {ev['missing_share']:.2%} (the test is void above "
            f"{rule['max_missing_share']:.0%} at a check) |",
            f"| Data counted up to | {fmt(ev['resolved_to'])} UTC |", '']
    if ev['state'] in ('alive', 'dead', 'dead_futility'):
        out += [f"The numbers above are for the {n} trades the result rests on. Later trades are not counted.", '']
    out += ['| Coin | Long | Short | Total |', '|---|---|---|---|']
    for s in rule['symbols']:
        longs = sum(t['symbol'] == s and t['direction'] == 'LONG' for t in sample)
        shorts = sum(t['symbol'] == s and t['direction'] == 'SHORT' for t in sample)
        out.append(f"| {s} | {longs} | {shorts} | {longs + shorts} |")
    out.append('')
    try:
        out += descriptions(conn, rule, ev)
    except Exception as e:                       # a description must never cost the section
        log.exception('descriptions failed for rule %s', rule['name'])
        out += [f"Descriptions unavailable this week ({e!r}). The counts and status above are not affected.", '']
    return out
