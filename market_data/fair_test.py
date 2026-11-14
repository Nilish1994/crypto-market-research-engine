"""
Fair test of one Strategy-1 signal on one timeframe.

The Pine script has no exit, so each trade is held for a fixed number of candles:
  - enter at the OPEN of the candle after the signal candle (the alert fires at its close)
  - exit at the CLOSE of the Nth candle after the signal
  - costs: round-trip fee + the real Binance funding settled while the trade was open
  - no double counting: a signal is skipped while the previous trade is still open

Baseline: the same trade entered on random candles in the same trend (uptrend for
LR signals, downtrend for SR signals). LR/SR already require that trend, so beating
random trend entries is what shows the retest itself adds anything. Random entries
are drawn with replacement from every trend candle, 'runs' times.
"""
import bisect
import random
import statistics
from datetime import datetime, timezone

from market_data import settings, strategy1

SPLIT_YEAR = 2023                      # older vs newer half of the history


def _year(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year


class FundingLedger:
    """Cumulative funding so the sum over any time range is one subtraction."""

    def __init__(self, rows):
        self.times = [t for t, _ in rows]
        self.cum = [0.0]
        for _, r in rows:
            self.cum.append(self.cum[-1] + r)

    def paid_by_long(self, t_from, t_to):
        """Funding a long pays for settlements in (t_from, t_to]; negative means it received."""
        return self.cum[bisect.bisect_right(self.times, t_to)] - self.cum[bisect.bisect_right(self.times, t_from)]


def trade_result(candles, i, hold, direction, fee, funding: FundingLedger, dur):
    """Trade for a signal on candle i, or None if the Nth candle does not exist yet."""
    if i + hold >= len(candles):
        return None
    entry, exit_ = candles[i + 1]['open'], candles[i + hold]['close']
    t_in, t_out = candles[i + 1]['open_time'], candles[i + hold]['open_time'] + dur - 1
    window = candles[i + 1:i + hold + 1]
    long_funding = funding.paid_by_long(t_in, t_out)
    if direction == 'LONG':
        gross, paid = exit_ / entry - 1, long_funding
        worst = min(c['low'] for c in window) / entry - 1
    else:
        gross, paid = 1 - exit_ / entry, -long_funding
        worst = 1 - max(c['high'] for c in window) / entry
    return {'open_time': candles[i]['open_time'], 'gross': gross, 'funding': paid,
            'net': gross - fee - paid, 'worst': worst}


def non_overlapping(candles, indices, hold, direction, fee, funding, dur):
    trades, busy_until = [], -1
    for i in indices:
        if i <= busy_until:
            continue
        t = trade_result(candles, i, hold, direction, fee, funding, dur)
        if t:
            trades.append(t)
            busy_until = i + hold
    return trades


def _line(label, trades):
    if not trades:
        return f"| {label:<26} |    0 |        |          |          |       |"
    net = [t['net'] for t in trades]
    mean = statistics.fmean(net)
    sd = statistics.stdev(net) if len(net) > 1 else 0.0
    t_stat = mean / (sd / len(net) ** 0.5) if sd else 0.0
    return (f"| {label:<26} | {len(net):>4} | {100 * sum(x > 0 for x in net) / len(net):5.1f}% "
            f"| {100 * mean:+7.2f}% | {100 * statistics.median(net):+7.2f}% | {t_stat:+5.2f} |")


HEADER = ["| Group                      | Trades | Won | Avg net | Median net | t |",
          "|----------------------------|-----:|-------:|---------:|---------:|------:|"]


def run(conn, timeframe, signal, holds=(6, 12, 24, 42), runs=2000, fee_bps=settings.ROUND_TRIP_FEE_BPS, seed=7):
    direction = strategy1.direction(signal)
    want_trend = 'UP' if direction == 'LONG' else 'DOWN'
    dur = settings.TIMEFRAMES[timeframe]
    fee = fee_bps / 10_000
    rows = conn.execute(
        "SELECT c.open_time, c.open, c.high, c.low, c.close, i.ema50, i.ema200 FROM candles c JOIN indicators i "
        "USING(symbol, timeframe, open_time) WHERE c.symbol=? AND c.timeframe=? ORDER BY c.open_time",
        (settings.SYMBOL, timeframe)).fetchall()
    candles = [dict(r) for r in rows]
    index = {c['open_time']: k for k, c in enumerate(candles)}
    funding = FundingLedger(conn.execute(
        "SELECT funding_time, rate FROM funding WHERE symbol=? ORDER BY funding_time", (settings.SYMBOL,)).fetchall())
    sig_idx = [index[r[0]] for r in conn.execute(
        "SELECT open_time FROM signals WHERE symbol=? AND timeframe=? AND signal=? ORDER BY open_time",
        (settings.SYMBOL, timeframe, signal))]
    trend_idx = [k for k, c in enumerate(candles) if c['ema50'] is not None and c['ema200'] is not None
                 and (c['ema50'] > c['ema200'] if direction == 'LONG' else c['ema50'] < c['ema200'])]

    out = [f"# Fair test: {signal} ({direction}) on {settings.SYMBOL} {timeframe}, Binance USD-M Futures",
           "",
           f"Entry at the next candle's open, exit after N candles, fee {fee_bps:g} bps round trip plus real funding,",
           f"no overlapping trades. Baseline: random entries on {'uptrend' if direction == 'LONG' else 'downtrend'} "
           f"candles ({len(trend_idx):,} of them), {runs} runs.",
           ""]
    if not sig_idx:
        return "\n".join(out + ["No signals stored for this timeframe."])
    if not funding.times:
        out += ["WARNING: no funding history stored; run `sync` first. Funding is counted as zero.", ""]

    rng = random.Random(seed)
    out += ["## Holding time",
            "",
            "| Hold | Trades | Won | Avg net | Random trend entries | Beats random in | p |",
            "|------|-----:|-------:|---------:|---------:|-------:|------:|"]
    for hold in holds:
        trades = non_overlapping(candles, sig_idx, hold, direction, fee, funding, dur)
        if not trades:
            out.append(f"| {hold:>3} | 0 | | | | | |")
            continue
        observed = statistics.fmean(t['net'] for t in trades)
        pool = [t['net'] for t in (trade_result(candles, i, hold, direction, fee, funding, dur) for i in trend_idx) if t]
        sims = [statistics.fmean(rng.choices(pool, k=len(trades))) for _ in range(runs)]
        p = sum(s >= observed for s in sims) / runs
        won = 100 * sum(t['net'] > 0 for t in trades) / len(trades)
        out.append(f"| {hold:>3} candles | {len(trades):>4} | {won:5.1f}% | {100 * observed:+7.2f}% "
                   f"| {100 * statistics.fmean(pool):+7.2f}% | {100 * (1 - p):5.1f}% | {p:.3f} |")
    out += ["",
            "'Beats random in' = share of random runs with a lower average. About 95% or more (p ≤ 0.05) on a",
            "holding time chosen in advance is the usual bar; trying several holding times makes a lucky pass likelier.",
            ""]

    hold = 12 if 12 in holds else holds[0]
    trades = non_overlapping(candles, sig_idx, hold, direction, fee, funding, dur)
    baseline = non_overlapping(candles, trend_idx, hold, direction, fee, funding, dur)
    out += [f"## Hold {hold} candles: by period and market", ""] + HEADER
    out.append(_line("All", trades))
    out.append(_line(f"Before {SPLIT_YEAR}", [t for t in trades if _year(t['open_time']) < SPLIT_YEAR]))
    out.append(_line(f"{SPLIT_YEAR} onwards", [t for t in trades if _year(t['open_time']) >= SPLIT_YEAR]))
    for y in sorted({_year(t['open_time']) for t in trades}):
        out.append(_line(f"  {y}", [t for t in trades if _year(t['open_time']) == y]))
    if timeframe not in ('1d', '1w'):
        d1 = {r[0]: r[1] for r in conn.execute(
            "SELECT open_time, ctx_trend FROM signal_context WHERE symbol=? AND timeframe=? AND signal=? AND context_tf='1d'",
            (settings.SYMBOL, timeframe, signal))}
        for tr in ('UP', 'DOWN', None):
            out.append(_line(f"Daily trend {tr or 'unknown'}", [t for t in trades if d1.get(t['open_time']) == tr]))
    out.append(_line("Baseline: every trend candle", baseline))
    out.append(_line(f"  baseline before {SPLIT_YEAR}", [t for t in baseline if _year(t['open_time']) < SPLIT_YEAR]))
    out.append(_line(f"  baseline {SPLIT_YEAR} onwards", [t for t in baseline if _year(t['open_time']) >= SPLIT_YEAR]))
    out += ["",
            f"Average funding paid per trade: {100 * statistics.fmean(t['funding'] for t in trades):+.3f}%; "
            f"median worst dip during a trade: {100 * statistics.median(t['worst'] for t in trades):.2f}%.",
            "Small groups (under ~30 trades) are noise, not findings."]
    return "\n".join(out)
