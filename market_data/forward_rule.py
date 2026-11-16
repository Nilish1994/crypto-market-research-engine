"""
Forward test of one frozen signal rule (settings.FROZEN_SIGNAL_RULES), written for rule s1h12.

Research only: it reads closed candles, Strategy-1 signals and funding that the engine already stores,
and works out what a fixed-rule trade would have returned. No orders, no account, no API keys.

The rule (frozen text: docs/architecture/evidence/RESEARCH-ENGINE-001/frozen-rule-s1h12.md):
  signal   a Strategy-1 signal on a closed candle that OPENS at or after frozen_ms (LR = long, SR = short)
  entry    open of the next candle
  exit     close of the Nth candle after the signal candle (time exit only, no stop, no target)
  costs    a fixed round trip plus the real funding settled while the trade is open
  one position per coin: a signal whose candle opens before the open trade's exit candle is skipped
  all coins are pooled into one sample, ordered by signal time

A trade is counted once (a) every coin's candles and signals are stored up to its exit, (b) its N candles
exist without a gap and (c) a funding settlement at or after its exit is stored. Trades are counted strictly
in order, so "the first 300" and "the first 600" do not depend on when a report is written.

The verdict (evaluate):
  nothing before `futility_trades` counted trades
  at `futility_trades`: dead if the mean net return of the first that many trades is <= 0
  one test on the first `target_trades` trades, or at the cap on the trades that exited by then:
      H0 mean net <= 0, one-sided, standard error clustered by ISO week of entry,
      t distribution with (weeks - 1) degrees of freedom
  void if the code pinned in the rule changed, or more than `max_missing_share` of candles are missing

This file is one of the pinned files: changing it after the start voids the test (see code_sha256).
The scorecard text and the descriptive comparisons live in forward_report.py, which is not pinned.
"""
import calendar
import hashlib
import math
import statistics
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path

from market_data import settings

PACKAGE_DIR = Path(__file__).resolve().parent
PINNED_FILES = ('strategy1.py', 'indicators.py', 'forward_rule.py')   # signal, EMAs, trade and verdict
MINUTE_MS = 60_000


# ── The rule's code and settings pin ──

def code_sha256() -> str:
    """One hash over the pinned files, in a fixed order."""
    h = hashlib.sha256()
    for name in PINNED_FILES:
        h.update(name.encode() + b'\0' + (PACKAGE_DIR / name).read_bytes() + b'\0')
    return h.hexdigest()


def pin_problems(rule) -> list:
    """Reasons the running engine no longer computes the rule as frozen (empty = all good)."""
    out = []
    if code_sha256() != rule['code_sha256']:
        out.append(f"the pinned code ({', '.join(PINNED_FILES)}) differs from the frozen version")
    if settings.RETEST_ZONE_PCT != rule['zone_pct']:
        out.append(f"the retest zone is {settings.RETEST_ZONE_PCT}, frozen {rule['zone_pct']}")
    if (settings.EMA_FAST, settings.EMA_SLOW) != (rule['ema_fast'], rule['ema_slow']):
        out.append(f"the EMAs are {settings.EMA_FAST}/{settings.EMA_SLOW}, frozen {rule['ema_fast']}/{rule['ema_slow']}")
    return out


def cap_ms(rule) -> int:
    """The hard cap: `cap_months` calendar months after the start."""
    d = datetime.fromtimestamp(rule['frozen_ms'] / 1000, tz=timezone.utc)
    months = d.month - 1 + rule['cap_months']
    year, month = d.year + months // 12, months % 12 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return int(d.replace(year=year, month=month, day=day).timestamp() * 1000)


# ── Data for one coin ──

class Funding:
    """Cumulative funding. Times are floored to the minute, as in the search harness (Binance stamps drift by ms)."""

    def __init__(self, rows):
        self.times = [t // MINUTE_MS * MINUTE_MS for t, _ in rows]
        self.cum = [0.0]
        for _, rate in rows:
            self.cum.append(self.cum[-1] + rate)

    def paid_by_long(self, t_from, t_to):
        """Funding a long pays for settlements in (t_from, t_to]; negative means it received."""
        return self.cum[bisect_right(self.times, t_to)] - self.cum[bisect_right(self.times, t_from)]

    @property
    def last(self):
        return self.times[-1] if self.times else None


class Market:
    """One coin's candles (with indicators), signals and funding from the rule's start on."""

    def __init__(self, conn, rule, symbol):
        tf, lo = rule['timeframe'], rule['frozen_ms']
        self.symbol, self.dur = symbol, settings.TIMEFRAMES[tf]
        self.candles = {r['open_time']: dict(r) for r in conn.execute(
            "SELECT c.open_time, c.open, c.close, c.volume, c.taker_buy_volume, i.ema50, i.ema200, i.volume_sma20 "
            "FROM candles c LEFT JOIN indicators i ON i.symbol=c.symbol AND i.timeframe=c.timeframe "
            "AND i.open_time=c.open_time WHERE c.symbol=? AND c.timeframe=? AND c.open_time>=?", (symbol, tf, lo))}
        # signals are written in the same commit as indicators, so this is how far the coin's signals are known
        self.known_through = conn.execute(
            "SELECT MAX(open_time) FROM indicators WHERE symbol=? AND timeframe=? AND open_time>=?",
            (symbol, tf, lo)).fetchone()[0]
        self.signals = {}
        for r in conn.execute("SELECT open_time, signal FROM signals WHERE symbol=? AND timeframe=? AND open_time>=? "
                              "ORDER BY open_time, signal", (symbol, tf, lo)):
            self.signals.setdefault(r['open_time'], []).append(r['signal'])
        # funding from one day before the start is plenty: only settlements after an entry are summed
        self.funding = Funding(conn.execute(
            "SELECT funding_time, rate FROM funding WHERE symbol=? AND funding_time>=? ORDER BY funding_time",
            (symbol, lo - 86_400_000)).fetchall())


def signal_direction(rule, names):
    """LONG, SHORT, or None if the candle's signals are not all on one side (cannot happen: opposite trends)."""
    sides = {'LONG' if n in rule['long_signals'] else 'SHORT' if n in rule['short_signals'] else None for n in names}
    return sides.pop() if len(sides) == 1 else None


def measure(rule, mkt: Market, signal_time, direction):
    """
    The rule's trade for a signal candle opening at signal_time, or the reason it cannot be measured yet:
    ('pending', None) the candles or funding are not stored yet; ('gap', None) a candle of its window is missing
    for good; ('ok', {...}) otherwise. Returns are fractions of notional.
    """
    dur, hold = mkt.dur, rule['hold']
    entry_time, exit_open = signal_time + dur, signal_time + hold * dur
    exit_close = exit_open + dur
    if mkt.known_through is None or mkt.known_through < exit_open:
        return 'pending', None
    if any(signal_time + k * dur not in mkt.candles for k in range(1, hold + 1)):
        return 'gap', None                     # candles are downloaded in order, so a hole is never filled later
    if mkt.funding.last is None or mkt.funding.last < exit_close:
        return 'pending', None                 # funding is downloaded in order: this proves the window is complete
    entry, exit_ = mkt.candles[entry_time]['open'], mkt.candles[exit_open]['close']
    sign = 1 if direction == 'LONG' else -1
    gross = sign * (exit_ / entry - 1)
    funding = sign * mkt.funding.paid_by_long(entry_time, exit_close - 1)
    cost = rule['cost_bps'] / 10_000
    return 'ok', {'entry': entry, 'exit': exit_, 'gross': gross, 'funding': funding, 'cost': cost,
                  'net': gross - cost - funding}


def taken_trades(rule, mkt: Market):
    """(trades the rule takes on this coin, signals skipped because a position was open), in time order."""
    dur, hold = mkt.dur, rule['hold']
    trades, skipped, busy_until = [], 0, None       # busy_until = open time of the open trade's exit candle
    for t in sorted(mkt.signals):
        direction = signal_direction(rule, mkt.signals[t])
        if direction is None:
            continue
        if busy_until is not None and t < busy_until:
            skipped += 1
            continue
        busy_until = t + hold * dur
        trades.append({'symbol': mkt.symbol, 'signal_time': t, 'signals': tuple(mkt.signals[t]),
                       'direction': direction, 'entry_time': t + dur, 'exit_close': busy_until + dur})
    return trades, skipped


def missing_share(conn, rule, hi) -> float:
    """Share of the rule's candles that are missing between the start and hi (exclusive), all coins together."""
    dur = settings.TIMEFRAMES[rule['timeframe']]
    expected = max(0, (hi - rule['frozen_ms']) // dur) * len(rule['symbols'])
    if not expected:
        return 0.0
    marks = ','.join('?' * len(rule['symbols']))
    stored = conn.execute(
        f"SELECT COUNT(*) FROM candles WHERE symbol IN ({marks}) AND timeframe=? AND open_time>=? AND open_time<?",
        (*rule['symbols'], rule['timeframe'], rule['frozen_ms'], hi)).fetchone()[0]
    return (expected - stored) / expected


# ── The single test ──

def _betacf(a, b, x):
    tiny = 1e-300
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > tiny else tiny)
    h = d
    for m in range(1, 1000):
        for aa in (m * (b - m) * x / ((qam + 2 * m) * (a + 2 * m)),
                   -(a + m) * (qab + m) * x / ((a + 2 * m) * (qap + 2 * m))):
            d = 1 + aa * d
            d = 1 / (d if abs(d) > tiny else tiny)
            c = 1 + aa / c
            c = c if abs(c) > tiny else tiny
            delta = d * c
            h *= delta
        if abs(delta - 1) < 1e-15:
            break
    return h


def _betai(a, b, x):
    """Regularised incomplete beta function I_x(a, b) (continued fraction, Numerical Recipes 6.4)."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log1p(-x))
    if x < (a + 1) / (a + b + 2):
        return front * _betacf(a, b, x) / a
    return 1 - front * _betacf(b, a, 1 - x) / b


def t_upper_tail(t, df) -> float:
    """P(T >= t) for a Student t distribution with df degrees of freedom."""
    half = 0.5 * _betai(df / 2, 0.5, df / (df + t * t))
    return half if t >= 0 else 1 - half


def iso_week(ms):
    return tuple(datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isocalendar())[:2]


def cluster_test(trades) -> dict:
    """
    One-sided test of "mean net > 0" with the standard error clustered by ISO week of entry (UTC):
      var(mean) = G/(G-1) * sum over weeks of (sum of the week's (net - mean))^2 / n^2,  t on G-1 degrees of freedom.
    p is None when there are fewer than two weeks.
    """
    n = len(trades)
    out = {'n': n, 'weeks': 0, 'mean': None, 'se': None, 't': None, 'p': None}
    if not n:
        return out
    mean = statistics.fmean(t['net'] for t in trades)
    sums = {}
    for t in trades:
        week = iso_week(t['entry_time'])
        sums[week] = sums.get(week, 0.0) + t['net'] - mean
    g = len(sums)
    out.update(weeks=g, mean=mean)
    if g < 2:
        return out
    se = math.sqrt(g / (g - 1) * sum(e * e for e in sums.values())) / n
    if se == 0:
        out.update(se=0.0, p=0.0 if mean > 0 else 1.0)
        return out
    out.update(se=se, t=mean / se, p=t_upper_tail(mean / se, g - 1))
    return out


# ── Counting and verdict ──

def evaluate(conn, rule, as_of_ms) -> dict:
    """
    The state of the forward test using only trades that exited by as_of_ms.

    state: not_started | running | dead_futility | alive | dead | void
    counted: every trade counted so far, in order.  sample: the trades the current state rests on.
    """
    frozen = rule['frozen_ms']
    if frozen is None or as_of_ms <= frozen:
        return {'state': 'not_started'}
    dur = settings.TIMEFRAMES[rule['timeframe']]
    markets = {s: Market(conn, rule, s) for s in rule['symbols']}
    cap = cap_ms(rule)
    # every coin's candles and signals are stored up to here; nothing later is counted yet
    known = [m.known_through for m in markets.values()]
    resolved_to = min(as_of_ms, min(known) + dur) if all(k is not None for k in known) else frozen

    order = {s: i for i, s in enumerate(rule['symbols'])}
    taken, skipped = [], 0
    for m in markets.values():
        trades, n = taken_trades(rule, m)
        taken += trades
        skipped += n
    taken.sort(key=lambda t: (t['signal_time'], order[t['symbol']]))

    counted, gaps, waiting_on = [], [], None
    for t in taken:
        if t['exit_close'] > resolved_to:
            break
        status, result = measure(rule, markets[t['symbol']], t['signal_time'], t['direction'])
        if status == 'pending':
            waiting_on = t                      # an earlier trade is not complete: later ones wait for it
            break
        if status == 'gap':
            gaps.append(t)
            continue
        counted.append({**t, **result})

    out = {'state': 'running', 'reason': None, 'cap_ms': cap, 'resolved_to': resolved_to, 'counted': counted,
           'sample': [t for t in counted if t['exit_close'] <= cap], 'taken': len(taken), 'skipped_busy': skipped,
           'left_out_gap': len(gaps), 'waiting_on': waiting_on, 'futility': None, 'test': None,
           'missing_share': missing_share(conn, rule, resolved_to), 'known_through': dict(zip(markets, known))}

    problems = pin_problems(rule)
    if problems:
        out.update(state='void', reason='; '.join(problems))
        return out

    limit = rule['max_missing_share']
    in_cap, n1, n2 = out['sample'], rule['futility_trades'], rule['target_trades']
    if len(in_cap) >= n1:
        first = in_cap[:n1]
        share = missing_share(conn, rule, first[-1]['exit_close'])
        out['futility'] = {'n': n1, 'mean': statistics.fmean(t['net'] for t in first), 'missing_share': share}
        if share > limit:
            out.update(state='void', sample=first, missing_share=share,
                       reason=f"{share:.2%} of candles were missing up to trade {n1}")
            return out
        if out['futility']['mean'] <= 0:
            out.update(state='dead_futility', sample=first, missing_share=share,
                       reason=f"mean net return of the first {n1} trades is not above zero")
            return out

    if len(in_cap) >= n2:
        final, window_end = in_cap[:n2], in_cap[n2 - 1]['exit_close']
    elif resolved_to >= cap and (waiting_on is None or waiting_on['exit_close'] > cap):
        final, window_end = in_cap, cap
    else:
        return out
    share = missing_share(conn, rule, window_end)
    out.update(sample=final, missing_share=share)
    if share > limit:
        out.update(state='void', reason=f"{share:.2%} of candles were missing in the test window")
        return out
    test = cluster_test(final)
    out['test'] = test
    if test['p'] is None:
        out.update(state='dead', reason='too few trades to test')
    elif test['p'] <= rule['alpha']:
        out.update(state='alive', reason=f"one-sided p = {test['p']:.4f} <= {rule['alpha']}")
    else:
        out.update(state='dead', reason=f"one-sided p = {test['p']:.4f} > {rule['alpha']}")
    return out
