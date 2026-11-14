"""
Strategy-1 "EMA Retest Zones (Eva Final Clean)", translated line for line from the owner's Pine script.

  upTrend   = ema50 > ema200;  downTrend = ema50 < ema200
  zone      = EMA ± 0.15%
  LR50/LR200: upTrend and close > EMA and zone_low <= low <= zone_high and close > open
  SR50/SR200: downTrend and close < EMA and zone_low <= high <= zone_high and close < open
  all on confirmed (closed) bars only

The script has no stop, target or exit; it only marks the candle.
"""
from market_data import settings

LONG_SIGNALS = ('LR50', 'LR200')
SHORT_SIGNALS = ('SR50', 'SR200')


def direction(signal: str) -> str:
    return 'LONG' if signal in LONG_SIGNALS else 'SHORT'


def signals_for(candle: dict, ema50, ema200, zone_pct: float = settings.RETEST_ZONE_PCT) -> list:
    """Return the Strategy-1 signals that fire on this closed candle (possibly several, possibly none)."""
    if ema50 is None or ema200 is None:      # Pine: comparisons with na are false
        return []
    o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
    up_trend = ema50 > ema200
    down_trend = ema50 < ema200
    fired = []
    for name_suffix, ma in (('50', ema50), ('200', ema200)):
        top = ma * (1 + zone_pct)
        bot = ma * (1 - zone_pct)
        if up_trend and c > ma and bot <= l <= top and c > o:
            fired.append('LR' + name_suffix)
        if down_trend and c < ma and bot <= h <= top and c < o:
            fired.append('SR' + name_suffix)
    order = {'LR50': 0, 'LR200': 1, 'SR50': 2, 'SR200': 3}
    return sorted(fired, key=order.get)
