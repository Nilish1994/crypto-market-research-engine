"""
EMA, RSI and volume SMA computed the way TradingView's Pine built-ins do.

  ta.ema(src, n): first value at bar n-1 = SMA of the first n values, then
                  ema = alpha*src + (1-alpha)*ema[1], alpha = 2/(n+1)
  ta.rma(src, n): same seeding, alpha = 1/n (Wilder smoothing)
  ta.rsi(close, n): rma of up-moves / rma of down-moves; the first change is
                  at bar 1, so the first RSI is at bar n
  ta.sma(volume, n)

Values are None until enough bars exist, like Pine's na.

The engine is stepped one candle at a time, so a full rebuild and an
incremental update produce identical numbers.
"""
from collections import deque
from dataclasses import dataclass, field

from market_data import settings


class _Ema:
    """Pine ta.ema / ta.rma with SMA seed."""

    def __init__(self, length: int, alpha: float):
        self.length = length
        self.alpha = alpha
        self.count = 0
        self.seed_sum = 0.0
        self.value = None

    def step(self, x: float):
        if self.value is None:
            self.count += 1
            self.seed_sum += x
            if self.count == self.length:
                self.value = self.seed_sum / self.length
        else:
            self.value = self.alpha * x + (1 - self.alpha) * self.value
        return self.value


def ema(length: int) -> _Ema:
    return _Ema(length, 2.0 / (length + 1))


def rma(length: int) -> _Ema:
    return _Ema(length, 1.0 / length)


def rsi_from_averages(avg_gain: float, avg_loss: float):
    """TradingView's RSI formula including its zero-division rules."""
    if avg_gain is None or avg_loss is None:
        return None
    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


@dataclass
class IndicatorEngine:
    """Running indicator state for one timeframe."""
    ema_fast: _Ema = field(default_factory=lambda: ema(settings.EMA_FAST))
    ema_slow: _Ema = field(default_factory=lambda: ema(settings.EMA_SLOW))
    avg_gain: _Ema = field(default_factory=lambda: rma(settings.RSI_LENGTH))
    avg_loss: _Ema = field(default_factory=lambda: rma(settings.RSI_LENGTH))
    volumes: deque = field(default_factory=lambda: deque(maxlen=settings.VOLUME_SMA_LENGTH))
    prev_close: float = None
    bars_seen: int = 0

    def step(self, candle: dict) -> dict:
        close = candle['close']
        if self.prev_close is not None:
            change = close - self.prev_close
            self.avg_gain.step(max(change, 0.0))
            self.avg_loss.step(max(-change, 0.0))
        self.prev_close = close
        self.volumes.append(candle['volume'])
        self.bars_seen += 1
        return {
            'open_time': candle['open_time'],
            'ema50': self.ema_fast.step(close),
            'ema200': self.ema_slow.step(close),
            'rsi14': rsi_from_averages(self.avg_gain.value, self.avg_loss.value),
            'rsi_avg_gain': self.avg_gain.value,
            'rsi_avg_loss': self.avg_loss.value,
            'volume_sma20': (sum(self.volumes) / len(self.volumes)
                             if len(self.volumes) == self.volumes.maxlen else None),
            'bars_seen': self.bars_seen,
        }

    @classmethod
    def resume(cls, last_row: dict, last_close: float, recent_volumes: list) -> 'IndicatorEngine':
        """
        Rebuild the engine from the last stored indicator row.

        Only valid once every indicator is warmed up; callers rebuild from the
        first candle otherwise.
        """
        engine = cls()
        engine.ema_fast.value = last_row['ema50']
        engine.ema_slow.value = last_row['ema200']
        engine.avg_gain.value = last_row['rsi_avg_gain']
        engine.avg_loss.value = last_row['rsi_avg_loss']
        engine.volumes.extend(recent_volumes[-settings.VOLUME_SMA_LENGTH:])
        engine.prev_close = last_close
        engine.bars_seen = last_row['bars_seen']
        return engine

    @staticmethod
    def can_resume(last_row: dict) -> bool:
        return last_row is not None and all(
            last_row[k] is not None for k in ('ema50', 'ema200', 'rsi_avg_gain', 'rsi_avg_loss', 'volume_sma20'))


def trend(ema50, ema200):
    """Strategy-1 trend: UP when EMA50 > EMA200, DOWN when below, None if not warmed up or equal."""
    if ema50 is None or ema200 is None or ema50 == ema200:
        return None
    return 'UP' if ema50 > ema200 else 'DOWN'
