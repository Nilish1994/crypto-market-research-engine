"""
Forward test of frozen signal rule s1h12 (market_data/forward_rule.py, forward_report.py).

Synthetic data only: candles, indicators, signals and funding are written straight into an in-memory
database, so every number below can be checked by hand. No network, no production database.
"""
import math
import random
import statistics
from datetime import datetime, timezone

import pytest

from market_data import binance_futures, forward_report, forward_rule, pipeline, reports, settings, store

HOUR = settings.TIMEFRAMES['1h']
DAY = 24 * HOUR
T0 = int(datetime(2027, 1, 4, tzinfo=timezone.utc).timestamp() * 1000)       # a Monday, 00:00 UTC
COINS = ('BTCUSDT', 'ETHUSDT', 'SOLUSDT')
COST = 0.0014
S1H12 = settings.FROZEN_SIGNAL_RULES[0]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(settings, 'REQUEST_PAUSE_SEC', 0)
    monkeypatch.setattr(binance_futures, 'fetch_funding', lambda start, get_json=None: iter(()))


def rule(**changes):
    """s1h12 with a start time and (for the gates) smaller numbers; everything else exactly as frozen."""
    return {**S1H12, 'frozen_ms': T0, **changes}


def make_db(hours, coins=COINS, opens=None, closes=None, missing=(), first=0, funding_at=None):
    """
    Flat candles (open = close = 100) for hours [first, hours) after T0 on each coin, in an uptrend
    (EMA50 101 > EMA200 100). opens/closes: {(coin, hour): price}. missing: {(coin, hour)} not stored.
    One zero funding settlement per coin at funding_at (default: the end), so finished trades count.
    """
    conn = store.connect(':memory:')
    opens, closes = opens or {}, closes or {}
    for coin in coins:
        for k in range(first, hours):
            if (coin, k) in missing:
                continue
            o, c = opens.get((coin, k), 100.0), closes.get((coin, k), 100.0)
            t = T0 + k * HOUR
            store.insert_candles(conn, coin, '1h', [{
                'open_time': t, 'close_time': t + HOUR - 1, 'open': o, 'high': max(o, c), 'low': min(o, c), 'close': c,
                'volume': 10.0, 'quote_volume': 0.0, 'trades': 1, 'taker_buy_volume': 4.0, 'taker_buy_quote_volume': 0.0}])
            conn.execute("INSERT INTO indicators VALUES (?,?,?,?,?,?,?,?,?,?)",
                         (coin, '1h', t, 101.0, 100.0, 50.0, 1.0, 1.0, 10.0, 300))
        add_funding(conn, coin, T0 + (hours if funding_at is None else funding_at) * HOUR, 0.0)
    conn.commit()
    return conn


def add_signal(conn, coin, hour, name='LR50'):
    direction = 'LONG' if name.startswith('LR') else 'SHORT'
    conn.execute("INSERT INTO signals VALUES (?,?,?,?,?,?,?)", (coin, '1h', T0 + hour * HOUR, name, direction, 100.0, 100.0))


def add_funding(conn, coin, t, rate):
    conn.execute("INSERT OR REPLACE INTO funding VALUES (?,?,?)", (coin, t, rate))


def trades_db(nets, coin='BTCUSDT', gap_hours=24, start_hour=1, extra_hours=30):
    """One long trade per entry of `nets` (the wanted NET return), signals gap_hours apart on one coin."""
    closes = {(coin, start_hour + k * gap_hours + 12): 100.0 * (1 + n + COST) for k, n in enumerate(nets)}
    conn = make_db(start_hour + len(nets) * gap_hours + extra_hours, coins=(coin,), closes=closes)
    for k in range(len(nets)):
        add_signal(conn, coin, start_hour + k * gap_hours)
    return conn


def ev(conn, r, hours):
    return forward_rule.evaluate(conn, r, T0 + hours * HOUR)


# ── The frozen rule ──

@pytest.mark.unit
class TestFrozenValues:
    def test_s1h12_is_stored_exactly_as_frozen(self):
        assert len(settings.FROZEN_SIGNAL_RULES) == 1
        frozen = dict(S1H12)
        start, code = frozen.pop('frozen_ms'), frozen.pop('code_sha256')
        assert frozen == {'name': 's1h12', 'symbols': ('BTCUSDT', 'ETHUSDT', 'SOLUSDT'), 'timeframe': '1h',
                          'long_signals': ('LR50', 'LR200'), 'short_signals': ('SR50', 'SR200'),
                          'zone_pct': 0.0015, 'ema_fast': 50, 'ema_slow': 200, 'hold': 12, 'cost_bps': 14,
                          'futility_trades': 300, 'target_trades': 600, 'cap_months': 24, 'alpha': 0.05,
                          'max_missing_share': 0.02}
        assert code == forward_rule.code_sha256() and len(code) == 64
        if start is not None:                     # set at deploy: must be a Monday, 00:00 UTC
            d = datetime.fromtimestamp(start / 1000, tz=timezone.utc)
            assert (d.weekday(), d.hour, d.minute, d.second, start % 1000) == (0, 0, 0, 0, 0)

    def test_frozen_values_agree_with_the_engine_settings(self):
        assert forward_rule.pin_problems(S1H12) == []
        assert (settings.RETEST_ZONE_PCT, settings.EMA_FAST, settings.EMA_SLOW) == (0.0015, 50, 200)
        assert forward_rule.PINNED_FILES == ('strategy1.py', 'indicators.py', 'forward_rule.py')

    def test_v1b_is_untouched(self):
        assert settings.FROZEN_RULES == ({'name': 'v1b', 'frozen_ms': 1793706420000, 'sources': ('swing_4h', 'swing_1d'),
                                          'support_rsi_below': 30, 'resistance_rsi_above': 70, 'min_visits': 100},)
        assert settings.ROUND_TRIP_FEE_BPS == 10

    def test_cap_is_24_calendar_months_after_the_start(self):
        assert forward_rule.cap_ms(rule()) == int(datetime(2029, 1, 4, tzinfo=timezone.utc).timestamp() * 1000)
        leap = int(datetime(2028, 2, 29, tzinfo=timezone.utc).timestamp() * 1000)
        assert forward_rule.cap_ms(rule(frozen_ms=leap)) == int(datetime(2030, 2, 28, tzinfo=timezone.utc).timestamp() * 1000)


# ── Not started ──

@pytest.mark.unit
class TestNotStarted:
    def test_with_no_start_time_nothing_is_counted_and_there_is_no_section(self):
        unset = {**S1H12, 'frozen_ms': None}
        conn = trades_db([0.01] * 5)
        assert forward_rule.evaluate(conn, unset, T0 + 400 * DAY) == {'state': 'not_started'}
        assert forward_report.section(conn, unset, T0 + 400 * DAY) == []

    def test_nothing_before_the_start_time(self):
        conn = trades_db([0.01] * 5)
        assert ev(conn, rule(), 0) == {'state': 'not_started'}
        assert forward_report.section(conn, rule(), T0) == []

    def test_weekly_scorecard_is_unchanged_while_the_rule_has_no_start_time(self, monkeypatch):
        conn = trades_db([0.01] * 5)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('engine_start', ?)", (str(T0),))
        monkeypatch.setattr(settings, 'FROZEN_SIGNAL_RULES', ({**S1H12, 'frozen_ms': None},))
        with_rule = reports.weekly_report(conn, T0)
        monkeypatch.setattr(settings, 'FROZEN_SIGNAL_RULES', ())
        assert reports.weekly_report(conn, T0) == with_rule and 's1h12' not in with_rule
        assert '## Rule v1b' in with_rule

    def test_weekly_scorecard_gets_the_section_next_to_v1b_once_started(self, monkeypatch):
        conn = trades_db([0.01] * 5)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('engine_start', ?)", (str(T0),))
        monkeypatch.setattr(settings, 'FROZEN_SIGNAL_RULES', (rule(symbols=('BTCUSDT',)),))
        text = reports.weekly_report(conn, T0)
        assert text.index('## Rule v1b') < text.index('## Rule s1h12') < text.index('## Forward evidence')


# ── From signal to trade ──

@pytest.mark.unit
class TestTrade:
    def test_long_enters_at_the_next_open_and_exits_at_the_close_of_the_12th_candle(self):
        conn = make_db(40, opens={('BTCUSDT', 6): 100.0}, closes={('BTCUSDT', 17): 103.0, ('BTCUSDT', 16): 150.0,
                                                                     ('BTCUSDT', 18): 50.0})
        add_signal(conn, 'BTCUSDT', 5)
        (t,) = ev(conn, rule(), 40)['counted']
        assert (t['symbol'], t['direction'], t['signals']) == ('BTCUSDT', 'LONG', ('LR50',))
        assert t['signal_time'] == T0 + 5 * HOUR and t['entry_time'] == T0 + 6 * HOUR
        assert t['exit_close'] == T0 + 18 * HOUR                     # the moment candle 17 (the 12th) closes
        assert (t['entry'], t['exit']) == (100.0, 103.0)
        assert t['gross'] == pytest.approx(0.03) and t['funding'] == 0
        assert t['net'] == pytest.approx(0.03 - 0.0014)

    def test_short_is_mirrored(self):
        conn = make_db(40, closes={('ETHUSDT', 17): 103.0})
        add_signal(conn, 'ETHUSDT', 5, 'SR200')
        (t,) = ev(conn, rule(), 40)['counted']
        assert t['direction'] == 'SHORT' and t['gross'] == pytest.approx(-0.03)
        assert t['net'] == pytest.approx(-0.03 - 0.0014)

    def test_entry_is_the_open_not_the_signal_close(self):
        conn = make_db(40, opens={('BTCUSDT', 6): 101.0}, closes={('BTCUSDT', 17): 103.02})
        add_signal(conn, 'BTCUSDT', 5)
        (t,) = ev(conn, rule(), 40)['counted']
        assert t['entry'] == 101.0 and t['gross'] == pytest.approx(103.02 / 101.0 - 1)

    def test_two_signals_on_one_candle_are_one_trade(self):
        conn = make_db(40)
        add_signal(conn, 'BTCUSDT', 5, 'LR50')
        add_signal(conn, 'BTCUSDT', 5, 'LR200')
        e = ev(conn, rule(), 40)
        assert [t['signals'] for t in e['counted']] == [('LR200', 'LR50')] and e['skipped_busy'] == 0

    def test_other_timeframes_and_unknown_signals_are_ignored(self):
        conn = make_db(40)
        conn.execute("INSERT INTO signals VALUES ('BTCUSDT','4h',?,?,?,?,?)", (T0 + 4 * HOUR, 'LR50', 'LONG', 100.0, 100.0))
        conn.execute("INSERT INTO signals VALUES ('BTCUSDT','1h',?,?,?,?,?)", (T0 + 4 * HOUR, 'XX', 'LONG', 100.0, 100.0))
        conn.execute("INSERT INTO signals VALUES ('DOGEUSDT','1h',?,?,?,?,?)", (T0 + 4 * HOUR, 'LR50', 'LONG', 100.0, 100.0))
        e = ev(conn, rule(), 40)
        assert e['counted'] == [] and e['taken'] == 0

    def test_trade_equals_the_engines_own_outcome_rows_on_a_full_pipeline(self, monkeypatch):
        rng, price, candles = random.Random(5), 30000.0, []
        for i in range(700):                                    # a random walk; the pipeline finds its own signals
            o = price
            c = o * (1 + rng.gauss(0, 0.004))
            candles.append({'open_time': T0 - 400 * HOUR + i * HOUR, 'close_time': T0 - 399 * HOUR + i * HOUR - 1,
                            'open': o, 'high': max(o, c) * 1.002, 'low': min(o, c) * 0.998, 'close': c, 'volume': 5.0})
            price = c

        def get_json(params):
            rows = [c for c in candles if c['open_time'] >= params['startTime']] if params['interval'] == '1h' else []
            return [[c['open_time'], str(c['open']), str(c['high']), str(c['low']), str(c['close']), str(c['volume']),
                     c['close_time'], '0', 1, '0', '0', '0'] for c in rows[:params['limit']]]

        monkeypatch.setattr(settings, 'SYMBOL', 'BTCUSDT')
        conn = store.connect(':memory:')
        pipeline.sync(conn, now_ms=T0 + 300 * HOUR, get_json=get_json)
        add_funding(conn, 'BTCUSDT', T0 + 300 * HOUR, 0.0)
        counted = ev(conn, rule(symbols=('BTCUSDT',)), 300)['counted']
        assert len(counted) >= 3
        for t in counted:
            rows = conn.execute("SELECT direction, entry_price, next_open, close_change_pct FROM outcomes o JOIN signals s "
                                "USING (symbol, timeframe, open_time, signal) WHERE o.symbol='BTCUSDT' AND o.timeframe='1h' "
                                "AND o.open_time=? AND o.horizon=12", (t['signal_time'],)).fetchall()
            assert rows and {r['direction'] for r in rows} == {t['direction']}
            sign = 1 if t['direction'] == 'LONG' else -1
            assert t['entry'] == rows[0]['next_open']
            assert t['exit'] == pytest.approx(rows[0]['entry_price'] * (1 + sign * rows[0]['close_change_pct'] / 100))
        stored = {r[0] for r in conn.execute("SELECT open_time FROM signals WHERE timeframe='1h' AND open_time>=?", (T0,))}
        assert {t['signal_time'] for t in counted} <= stored and min(stored) >= T0


@pytest.mark.unit
class TestFunding:
    def _one(self, name, settlements, hours=40, funding_at=None):
        conn = make_db(hours, coins=('BTCUSDT',), funding_at=funding_at)
        add_signal(conn, 'BTCUSDT', 5, name)                        # entry 06:00, the 12th candle closes at 18:00
        for hour, rate in settlements:
            add_funding(conn, 'BTCUSDT', T0 + int(hour * HOUR), rate)
        return ev(conn, rule(symbols=('BTCUSDT',)), hours)

    def test_long_pays_positive_funding_and_short_receives_it(self):
        (t,) = self._one('LR50', [(8, 0.0003), (16, -0.0001)])['counted']
        assert t['funding'] == pytest.approx(0.0002) and t['net'] == pytest.approx(-0.0014 - 0.0002)
        (t,) = self._one('SR50', [(8, 0.0003), (16, -0.0001)])['counted']
        assert t['funding'] == pytest.approx(-0.0002) and t['net'] == pytest.approx(-0.0014 + 0.0002)

    def test_settlements_exactly_at_entry_or_at_the_exit_close_are_not_charged(self):
        (t,) = self._one('LR50', [(6, 0.01), (18, 0.02)])['counted']
        assert t['funding'] == 0
        (t,) = self._one('LR50', [(6 + 1 / 60, 0.01), (18 - 1 / 60, 0.02)])['counted']      # one minute inside
        assert t['funding'] == pytest.approx(0.03)

    def test_settlement_times_are_floored_to_the_minute(self):
        conn = make_db(40, coins=('BTCUSDT',))
        add_signal(conn, 'BTCUSDT', 5)
        add_funding(conn, 'BTCUSDT', T0 + 6 * HOUR + 7, 0.01)        # Binance stamps drift by a few ms
        add_funding(conn, 'BTCUSDT', T0 + 18 * HOUR + 7, 0.02)
        (t,) = ev(conn, rule(symbols=('BTCUSDT',)), 40)['counted']
        assert t['funding'] == 0

    def test_trade_waits_until_funding_at_or_after_its_exit_is_stored(self):
        e = self._one('LR50', [(16, 0.0001)], funding_at=17)        # last stored settlement is before the 18:00 exit
        assert e['counted'] == [] and e['taken'] == 1 and e['waiting_on']['signal_time'] == T0 + 5 * HOUR
        e = self._one('LR50', [(16, 0.0001)], funding_at=18)
        assert len(e['counted']) == 1 and e['waiting_on'] is None

    def test_no_funding_stored_at_all_means_nothing_is_counted(self):
        conn = make_db(40, coins=('BTCUSDT',))
        add_signal(conn, 'BTCUSDT', 5)
        conn.execute("DELETE FROM funding")
        assert ev(conn, rule(symbols=('BTCUSDT',)), 40)['counted'] == []


# ── One position per coin, start-time cut, order ──

@pytest.mark.unit
class TestPositions:
    def test_signals_while_a_trade_is_open_are_skipped_and_coins_are_separate(self):
        conn = make_db(80)
        for hour in (5, 6, 16, 17, 30):       # 5 opens; 6 and 16 are inside it; 17 is its exit candle: taken
            add_signal(conn, 'BTCUSDT', hour)
        add_signal(conn, 'ETHUSDT', 6)        # another coin is not blocked
        e = ev(conn, rule(), 80)
        got = [(t['symbol'], (t['signal_time'] - T0) // HOUR) for t in e['counted']]
        assert got == [('BTCUSDT', 5), ('ETHUSDT', 6), ('BTCUSDT', 17), ('BTCUSDT', 30)]
        assert e['skipped_busy'] == 2 and e['taken'] == 4

    def test_a_skipped_signal_does_not_extend_the_open_trade(self):
        conn = make_db(80)
        for hour in (5, 10, 18):              # 10 is skipped; if it had extended the trade, 18 would be skipped too
            add_signal(conn, 'BTCUSDT', hour)
        assert [(t['signal_time'] - T0) // HOUR for t in ev(conn, rule(), 80)['counted']] == [5, 18]

    def test_only_signal_candles_opening_at_or_after_the_start_count(self):
        conn = make_db(80, first=-20)
        for hour in (-13, -1, 0, 20):         # -1 would still be open at hour 0 if it counted, and block it
            add_signal(conn, 'BTCUSDT', hour)
        e = ev(conn, rule(), 80)
        assert [(t['signal_time'] - T0) // HOUR for t in e['counted']] == [0, 20] and e['skipped_busy'] == 0

    def test_pooled_sample_is_in_signal_time_order_then_coin_order(self):
        conn = make_db(80)
        for coin, hour in (('SOLUSDT', 3), ('BTCUSDT', 9), ('BTCUSDT', 3), ('ETHUSDT', 20), ('ETHUSDT', 2)):
            add_signal(conn, coin, hour)
        got = [(t['symbol'][:3], (t['signal_time'] - T0) // HOUR) for t in ev(conn, rule(), 80)['counted']]
        assert got == [('ETH', 2), ('BTC', 3), ('SOL', 3), ('ETH', 20)]      # BTC at 9 is skipped: trade open

    def test_trade_counts_only_once_its_12th_candle_has_closed(self):
        conn = make_db(40, coins=('BTCUSDT',), funding_at=200)
        add_signal(conn, 'BTCUSDT', 27)                               # 12th candle is hour 39, the last one stored
        r = rule(symbols=('BTCUSDT',))
        assert ev(conn, r, 39)['counted'] == []                      # as of 39:00 candle 39 has not closed
        assert len(ev(conn, r, 40)['counted']) == 1
        add_signal(conn, 'BTCUSDT', 28 + 12)                          # its window runs past the stored candles
        e = ev(conn, r, 400)
        assert len(e['counted']) == 1 and e['taken'] == 2

    def test_as_of_time_hides_later_trades(self):
        conn = trades_db([0.01, 0.02, 0.03])
        r = rule(symbols=('BTCUSDT',))
        assert [len(ev(conn, r, h)['counted']) for h in (13, 14, 37, 38, 500)] == [0, 1, 1, 2, 3]

    def test_a_coin_that_lags_holds_back_the_other_coins(self):
        conn = make_db(60, missing={('SOLUSDT', k) for k in range(20, 60)})        # SOL stops after hour 19
        conn.execute("DELETE FROM indicators WHERE symbol='SOLUSDT' AND open_time>=?", (T0 + 20 * HOUR,))
        add_signal(conn, 'BTCUSDT', 2)        # exits at 15:00: every coin is stored up to then
        add_signal(conn, 'ETHUSDT', 9)        # exits at 22:00: SOL signals up to then are not known yet
        e = ev(conn, rule(), 60)
        assert [t['symbol'] for t in e['counted']] == ['BTCUSDT'] and e['resolved_to'] == T0 + 20 * HOUR
        assert e['missing_share'] == 0        # a lagging coin is late, not missing

    def test_an_earlier_trade_waiting_for_funding_holds_back_later_trades(self):
        conn = make_db(60)
        add_signal(conn, 'BTCUSDT', 2)        # exits at 15:00
        add_signal(conn, 'ETHUSDT', 5)        # exits at 18:00, complete
        conn.execute("DELETE FROM funding WHERE symbol='BTCUSDT'")
        add_funding(conn, 'BTCUSDT', T0 + 10 * HOUR, 0.0)             # BTC funding stored only up to 10:00
        e = ev(conn, rule(), 60)
        assert e['counted'] == [] and e['waiting_on']['symbol'] == 'BTCUSDT' and e['taken'] == 2
        add_funding(conn, 'BTCUSDT', T0 + 16 * HOUR, 0.0)
        assert [t['symbol'] for t in ev(conn, rule(), 60)['counted']] == ['BTCUSDT', 'ETHUSDT']

    def test_a_coin_with_no_data_means_nothing_is_counted(self):
        conn = make_db(60, coins=('BTCUSDT', 'ETHUSDT'))
        add_signal(conn, 'BTCUSDT', 2)
        e = ev(conn, rule(), 60)
        assert e['counted'] == [] and e['state'] == 'running'


# ── Missing candles and the void conditions ──

@pytest.mark.unit
class TestGapsAndVoid:
    def test_trade_with_a_missing_candle_is_left_out_but_still_blocks(self):
        conn = make_db(80, coins=('BTCUSDT',), missing={('BTCUSDT', 10)})
        for hour in (5, 12, 17, 40):          # 5: candle 10 is missing; 12 would be skipped; 17 is taken
            add_signal(conn, 'BTCUSDT', hour)
        e = ev(conn, rule(symbols=('BTCUSDT',)), 80)
        assert [(t['signal_time'] - T0) // HOUR for t in e['counted']] == [17, 40]
        assert (e['left_out_gap'], e['skipped_busy'], e['taken']) == (1, 1, 3)

    def test_missing_share_counts_all_coins_from_the_start(self):
        conn = make_db(100, missing={('BTCUSDT', 10), ('ETHUSDT', 11), ('ETHUSDT', 99)})
        r = rule()
        assert forward_rule.missing_share(conn, r, T0 + 50 * HOUR) == pytest.approx(2 / 150)
        assert forward_rule.missing_share(conn, r, T0 + 100 * HOUR) == pytest.approx(3 / 300)
        assert forward_rule.missing_share(conn, r, T0) == 0

    def _gappy(self, missing_hours, nets=(0.01, 0.01, 0.01, 0.01)):
        closes = {('BTCUSDT', 1 + k * 24 + 12): 100.0 * (1 + n + COST) for k, n in enumerate(nets)}
        conn = make_db(200, coins=('BTCUSDT',), closes=closes, missing={('BTCUSDT', h) for h in missing_hours})
        for k in range(len(nets)):
            add_signal(conn, 'BTCUSDT', 1 + k * 24)
        return conn

    def test_more_than_2_percent_missing_at_the_300_trade_check_voids_the_test(self):
        r = rule(symbols=('BTCUSDT',), futility_trades=4, target_trades=8)
        # the 4th trade exits at hour 86: 2 missing of 86 = 2.3%, 1 missing = 1.2%
        e = ev(self._gappy([14, 15]), r, 200)
        assert e['state'] == 'void' and 'missing' in e['reason'] and e['test'] is None
        assert ev(self._gappy([14]), r, 200)['state'] == 'running'

    def test_void_takes_priority_over_a_dead_result(self):
        r = rule(symbols=('BTCUSDT',), futility_trades=4, target_trades=8)
        assert ev(self._gappy([14, 15], nets=(-0.01,) * 4), r, 200)['state'] == 'void'

    def test_more_than_2_percent_missing_in_the_final_window_voids_the_test(self):
        r = rule(symbols=('BTCUSDT',), futility_trades=2, target_trades=4)
        # the 2nd trade exits at hour 38 (nothing missing by then); the 4th at hour 86 with 2 of 86 missing
        e = ev(self._gappy([40, 41]), r, 200)
        assert e['futility']['missing_share'] == 0 and e['state'] == 'void' and e['test'] is None

    def test_exactly_at_the_limit_is_not_void(self):
        r = rule(symbols=('BTCUSDT',), futility_trades=4, target_trades=8, max_missing_share=1 / 86)
        assert ev(self._gappy([14]), r, 200)['state'] == 'running'

    def test_exactly_at_the_limit_in_the_final_window_is_not_void(self):
        r = rule(symbols=('BTCUSDT',), futility_trades=2, target_trades=4, max_missing_share=2 / 86)
        e = ev(self._gappy([40, 41]), r, 200)
        assert e['missing_share'] == 2 / 86 and e['state'] in ('alive', 'dead') and e['test']['n'] == 4

    def test_changed_pinned_code_voids_the_test(self, monkeypatch, tmp_path):
        conn = trades_db([0.01] * 3)
        r = rule(symbols=('BTCUSDT',))
        assert ev(conn, r, 200)['state'] == 'running'
        for name in forward_rule.PINNED_FILES:                        # a one-byte change to any pinned file
            for other in forward_rule.PINNED_FILES:
                text = (forward_rule.PACKAGE_DIR / other).read_bytes()
                (tmp_path / other).write_bytes(text + (b'\n' if other == name else b''))
            with monkeypatch.context() as m:
                m.setattr(forward_rule, 'PACKAGE_DIR', tmp_path)
                e = forward_rule.evaluate(conn, r, T0 + 200 * HOUR)
                assert e['state'] == 'void' and 'pinned code' in e['reason'], name
        assert ev(conn, r, 200)['state'] == 'running'

    def test_changed_zone_or_ema_settings_void_the_test(self, monkeypatch):
        conn = trades_db([0.01] * 3)
        r = rule(symbols=('BTCUSDT',))
        with monkeypatch.context() as m:
            m.setattr(settings, 'RETEST_ZONE_PCT', 0.002)
            assert ev(conn, r, 200)['state'] == 'void' and 'zone' in ev(conn, r, 200)['reason']
        with monkeypatch.context() as m:
            m.setattr(settings, 'EMA_SLOW', 100)
            assert ev(conn, r, 200)['state'] == 'void' and 'EMAs' in ev(conn, r, 200)['reason']
        assert ev(conn, r, 200)['state'] == 'running'


# ── The gates: nothing before 300, futility at 300, one test at 600 or at the cap ──

GATES = dict(symbols=('BTCUSDT',), futility_trades=4, target_trades=8)
WINNERS = [0.02, 0.03, 0.025, 0.035, 0.02, 0.03, 0.025, 0.035]        # 8 trades over 2 ISO weeks... see trades_db


def hours_for(n, gap_hours=24):
    """As-of hour at which exactly n trades of trades_db have exited."""
    return 1 + (n - 1) * gap_hours + 13 if n else 1


@pytest.mark.unit
class TestGates:
    def test_no_verdict_and_no_test_before_the_first_check(self):
        conn = trades_db([-0.05, -0.05, -0.05, 0.01, 0.01])
        e = ev(conn, rule(**GATES), hours_for(3))
        assert (e['state'], e['futility'], e['test'], len(e['counted'])) == ('running', None, None, 3)

    def test_dead_at_the_first_check_when_the_mean_is_not_above_zero(self):
        conn = trades_db([0.01, -0.02, 0.01, -0.01] + [0.5] * 8)       # first four average -0.25%
        r = rule(**GATES)
        e = ev(conn, r, hours_for(4))
        assert e['state'] == 'dead_futility' and e['futility']['mean'] == pytest.approx(-0.0025) and e['test'] is None
        later = ev(conn, r, hours_for(12))                              # big later winners do not revive it
        assert later['state'] == 'dead_futility' and len(later['sample']) == 4
        assert later['futility']['mean'] == pytest.approx(-0.0025)

    def test_a_mean_of_exactly_zero_is_dead(self):
        closes = {('BTCUSDT', 1 + k * 24 + 12): px for k, px in enumerate((101.0, 99.0, 102.0, 98.0))}
        conn = make_db(140, coins=('BTCUSDT',), closes=closes)
        for k in range(4):
            add_signal(conn, 'BTCUSDT', 1 + k * 24)
        e = ev(conn, rule(**GATES, cost_bps=0), 140)
        assert e['futility']['mean'] == 0 and e['state'] == 'dead_futility'

    def test_passing_the_first_check_gives_no_verdict_and_no_test_until_the_target(self):
        conn = trades_db(WINNERS)
        r = rule(**GATES)
        for n in (4, 5, 6, 7):
            e = ev(conn, r, hours_for(n))
            assert (e['state'], e['test'], len(e['sample'])) == ('running', None, n)
            assert e['futility']['mean'] == pytest.approx(statistics.fmean(WINNERS[:4]))    # always the first four

    def test_one_test_at_the_target_on_exactly_the_first_trades(self):
        conn = trades_db(WINNERS + [-0.9, -0.9])
        r = rule(**GATES)
        e = ev(conn, r, hours_for(8))
        assert e['state'] == 'alive' and e['test']['n'] == 8 and e['test']['p'] <= 0.05
        assert e['test']['mean'] == pytest.approx(statistics.fmean(WINNERS))
        later = ev(conn, r, hours_for(10))                              # two huge later losers change nothing
        assert later['state'] == 'alive' and later['test'] == e['test'] and len(later['sample']) == 8
        assert len(later['counted']) == 10

    def test_dead_at_the_target_when_p_is_above_alpha(self):
        nets = [0.03, -0.02, 0.025, -0.02, 0.03, -0.025, 0.02, -0.015]   # positive mean, far from significant
        e = ev(trades_db(nets), rule(**GATES), hours_for(8))
        assert e['futility']['mean'] > 0 and e['state'] == 'dead' and e['test']['p'] > 0.05

    def test_alpha_is_the_bar(self):
        conn = trades_db(WINNERS)
        p = ev(conn, rule(**GATES), hours_for(8))['test']['p']
        assert ev(conn, rule(**GATES, alpha=p), hours_for(8))['state'] == 'alive'          # p <= alpha
        assert ev(conn, rule(**GATES, alpha=p * 0.999), hours_for(8))['state'] == 'dead'

    def test_real_gates_are_300_and_600(self):
        nets = [0.004 if k % 3 else -0.002 for k in range(600)]
        conn = trades_db(nets, gap_hours=13, extra_hours=20)
        r = rule(symbols=('BTCUSDT',))
        at = lambda n: ev(conn, r, hours_for(n, 13))
        assert (at(299)['state'], at(299)['futility'], at(299)['test']) == ('running', None, None)
        assert at(300)['futility']['n'] == 300 and at(300)['state'] == 'running' and at(300)['test'] is None
        assert at(599)['test'] is None and at(599)['state'] == 'running'
        assert at(600)['test']['n'] == 600 and at(600)['state'] == 'alive'
        losing = trades_db([-x for x in nets[:300]], gap_hours=13, extra_hours=20)
        assert ev(losing, r, hours_for(299, 13))['state'] == 'running'
        assert ev(losing, r, hours_for(300, 13))['state'] == 'dead_futility'


@pytest.mark.unit
class TestCap:
    R = dict(symbols=('BTCUSDT',), futility_trades=4, target_trades=8, cap_months=1)     # cap = T0 + 31 days
    CAP_H = 31 * 24

    def test_at_the_cap_the_test_runs_on_the_trades_that_exited_by_then(self):
        nets = [0.02, 0.03, 0.025, 0.035, 0.03]                       # 48 hours apart: four in week 1, one in week 2
        closes = {('BTCUSDT', 1 + k * 48 + 12): 100.0 * (1 + n + COST) for k, n in enumerate(nets)}
        closes[('BTCUSDT', self.CAP_H - 5 + 12)] = 10.0               # a disaster that exits after the cap
        conn = make_db(self.CAP_H + 40, coins=('BTCUSDT',), closes=closes)
        for k in range(len(nets)):
            add_signal(conn, 'BTCUSDT', 1 + k * 48)
        add_signal(conn, 'BTCUSDT', self.CAP_H - 5)
        r = rule(**self.R)
        assert forward_rule.cap_ms(r) == T0 + self.CAP_H * HOUR
        before = ev(conn, r, self.CAP_H - 1)
        assert (before['state'], before['test']) == ('running', None)
        e = ev(conn, r, self.CAP_H + 40)
        assert len(e['counted']) == 6 and e['test']['n'] == 5 and e['state'] == 'alive'
        assert e['test']['mean'] == pytest.approx(statistics.fmean(nets))

    def test_too_few_trades_at_the_cap_is_no_edge_shown(self):
        conn = trades_db([0.05, 0.05], extra_hours=self.CAP_H)        # both in one ISO week
        e = ev(conn, rule(**self.R), self.CAP_H + 10)
        assert e['state'] == 'dead' and e['test']['p'] is None and 'too few' in e['reason']

    def test_the_cap_waits_for_an_unfinished_trade_that_exited_before_it(self):
        conn = trades_db([0.02, 0.03, 0.025], extra_hours=self.CAP_H + 20)
        conn.execute("DELETE FROM funding")
        add_funding(conn, 'BTCUSDT', T0 + 40 * HOUR, 0.0)             # funding stored only up to hour 40
        e = ev(conn, rule(**self.R), self.CAP_H + 20)
        assert e['state'] == 'running' and len(e['counted']) == 2 and e['waiting_on'] is not None

    def test_target_reached_before_the_cap_is_tested_on_the_first_trades(self):
        conn = trades_db(WINNERS + [0.01] * 3, extra_hours=self.CAP_H)
        e = ev(conn, rule(**self.R), self.CAP_H + 10)
        assert e['test']['n'] == 8 and len(e['sample']) == 8


# ── The single test: one-sided, clustered by ISO week ──

def week_trades(weeks):
    """weeks: list of lists of net returns; week k's trades enter on day 7k of 2027 (Mondays)."""
    return [{'net': x, 'entry_time': T0 + k * 7 * DAY + i * HOUR} for k, xs in enumerate(weeks) for i, x in enumerate(xs)]


@pytest.mark.unit
class TestClusterTest:
    def test_t_distribution_tail_matches_closed_forms_and_tables(self):
        for t in (-3.0, -0.5, 0.0, 0.3, 1.0, 2.5, 12.0):
            assert forward_rule.t_upper_tail(t, 1) == pytest.approx(0.5 - math.atan(t) / math.pi, abs=1e-12)
            assert forward_rule.t_upper_tail(t, 2) == pytest.approx(0.5 - t / (2 * math.sqrt(2 + t * t)), abs=1e-12)
        for df, crit in ((5, 2.015048), (10, 1.812461), (30, 1.697261), (60, 1.670649), (100, 1.660234)):
            assert forward_rule.t_upper_tail(crit, df) == pytest.approx(0.05, abs=2e-6)      # standard 5% table

    def test_t_distribution_tail_matches_numerical_integration_of_the_density(self):
        def tail(t, df, steps=4000):                                  # 0.5 - Simpson integral of the density on [0, t]
            k = math.exp(math.lgamma((df + 1) / 2) - math.lgamma(df / 2)) / math.sqrt(df * math.pi)
            f = lambda x: k * (1 + x * x / df) ** (-(df + 1) / 2)
            h = t / steps
            return 0.5 - h / 3 * (f(0) + f(t) + sum((4 if i % 2 else 2) * f(i * h) for i in range(1, steps)))
        for df in (3, 7, 25, 59, 78, 103, 250):
            for t in (0.2, 1.0, 1.66, 2.4, 4.0):
                assert forward_rule.t_upper_tail(t, df) == pytest.approx(tail(t, df), abs=1e-10)
                assert forward_rule.t_upper_tail(-t, df) == pytest.approx(1 - tail(t, df), abs=1e-10)

    def test_hand_computed_example(self):
        # weeks: [1%, 3%], [-1%], [2%, 2%, 1%]; mean = 8/6 %; week residual sums: 4-8/3, -1-4/3, 5-4 (in %)
        r = forward_rule.cluster_test(week_trades([[0.01, 0.03], [-0.01], [0.02, 0.02, 0.01]]))
        mean = 0.08 / 6
        sums = [0.04 - 2 * mean, -0.01 - mean, 0.05 - 3 * mean]
        se = math.sqrt(3 / 2 * sum(e * e for e in sums)) / 6
        assert (r['n'], r['weeks']) == (6, 3)
        assert r['mean'] == pytest.approx(mean) and r['se'] == pytest.approx(se) and r['t'] == pytest.approx(mean / se)
        assert r['p'] == pytest.approx(0.5 - (mean / se) / (2 * math.sqrt(2 + (mean / se) ** 2)))   # df = 2

    def test_one_trade_per_week_is_the_ordinary_t_test(self):
        xs = [0.01, -0.004, 0.02, 0.003, -0.01, 0.015, 0.007]
        r = forward_rule.cluster_test(week_trades([[x] for x in xs]))
        t = statistics.fmean(xs) / (statistics.stdev(xs) / math.sqrt(len(xs)))
        assert r['t'] == pytest.approx(t) and r['p'] == pytest.approx(forward_rule.t_upper_tail(t, 6))

    def test_trades_that_move_together_inside_a_week_widen_the_error(self):
        weeks = [[0.02] * 5, [-0.01] * 5, [0.03] * 5, [-0.02] * 5, [0.01] * 5, [0.015] * 5]
        clustered = forward_rule.cluster_test(week_trades(weeks))
        flat = [x for w in weeks for x in w]
        naive_se = statistics.stdev(flat) / math.sqrt(len(flat))
        assert clustered['se'] > 2 * naive_se and clustered['weeks'] == 6
        # identical to testing the six weekly means
        means = [w[0] for w in weeks]
        assert clustered['t'] == pytest.approx(statistics.fmean(means) / (statistics.stdev(means) / math.sqrt(6)))

    def test_weeks_follow_the_iso_calendar_in_utc_and_use_the_entry_time(self):
        sunday_2359 = T0 + 7 * DAY - 60_000
        assert forward_rule.iso_week(sunday_2359) == (2027, 1) and forward_rule.iso_week(sunday_2359 + 60_000) == (2027, 2)
        new_year = int(datetime(2027, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        assert forward_rule.iso_week(new_year) == (2026, 53)          # ISO year, not calendar year
        r = forward_rule.cluster_test([{'net': 0.01, 'entry_time': sunday_2359},
                                       {'net': 0.02, 'entry_time': sunday_2359 + 60_000}])
        assert r['weeks'] == 2

    def test_the_test_is_one_sided(self):
        up = forward_rule.cluster_test(week_trades([[0.01], [0.02], [0.015], [0.012]]))
        down = forward_rule.cluster_test(week_trades([[-0.01], [-0.02], [-0.015], [-0.012]]))
        assert up['p'] < 0.01 and down['p'] == pytest.approx(1 - up['p'])

    def test_fewer_than_two_weeks_cannot_be_tested(self):
        assert forward_rule.cluster_test([])['p'] is None
        one = forward_rule.cluster_test(week_trades([[0.01, 0.02, 0.03]]))
        assert (one['weeks'], one['p'], one['mean']) == (1, None, pytest.approx(0.02))

    def test_evaluate_uses_the_clustered_test_on_the_sample(self):
        conn = trades_db(WINNERS)
        e = ev(conn, rule(**GATES), hours_for(8))
        assert e['test'] == forward_rule.cluster_test(e['sample'])
        assert e['test']['weeks'] == len({forward_rule.iso_week(t['entry_time']) for t in e['sample']}) == 2


# ── Scorecard section and descriptions ──

def section_text(conn, r, hours):
    return '\n'.join(forward_report.section(conn, r, T0 + hours * HOUR))


@pytest.mark.unit
class TestSection:
    def test_running_section_says_no_verdict_and_shows_the_fields(self):
        conn = make_db(120, closes={('BTCUSDT', 13): 102.0, ('ETHUSDT', 15): 101.0, ('BTCUSDT', 40): 99.0})
        add_signal(conn, 'BTCUSDT', 1)
        add_signal(conn, 'ETHUSDT', 3, 'SR50')
        add_signal(conn, 'BTCUSDT', 28)
        add_signal(conn, 'BTCUSDT', 30)                              # skipped
        add_signal(conn, 'SOLUSDT', 115)                             # still open
        add_funding(conn, 'BTCUSDT', T0 + 8 * HOUR, 0.0002)
        text = section_text(conn, rule(), 120)
        assert text.startswith('## Rule s1h12 — pre-registered forward test, started 2027-01-04 00:00 UTC')
        assert '**Status:** no verdict until 300 / 600 trades. Counted so far: 3.' in text
        assert 'Research only, not a trade signal.' in text
        assert '| Trades counted | 3 of 600 (0.5%) |' in text
        nets = [0.02 - COST - 0.0002, -0.01 - COST, -0.01 - COST]
        assert forward_report.bps(statistics.fmean(nets)) == '-14.7 bps' and '| Mean net per trade | -14.7 bps |' in text
        assert '| Mean gross per trade (before costs and funding) | +0.0 bps |' in text
        assert '| Fees and slippage | 14 bps per trade, 42 bps in total |' in text
        assert '| Funding paid (negative = received) | +0.67 bps per trade, +2.0 bps in total |' in text
        assert '| Signals skipped because the coin already had a trade open | 1 |' in text
        assert '| Trades taken but not counted yet (still open, or waiting for candles or funding) | 1 |' in text
        assert '| 1h candles missing since the start | 0.00%' in text
        assert '| BTCUSDT | 2 | 0 | 2 |' in text and '| ETHUSDT | 0 | 1 | 1 |' in text and '| SOLUSDT | 0 | 0 | 0 |' in text
        assert 'they have no say in the verdict' in text
        assert 'ALIVE' not in text and 'DEAD' not in text and 'p =' not in text

    def test_no_verdict_line_is_there_every_week_until_the_target(self):
        conn = trades_db(WINNERS)
        r = rule(**GATES)
        for n in range(0, 8):
            text = section_text(conn, r, hours_for(n))
            assert 'no verdict until 4 / 8 trades' in text and 'p =' not in text and 't =' not in text, n
            assert ('check is done and did not stop the test' in text) == (n >= 4)
        assert 'ALIVE: the single test passed' in section_text(conn, r, hours_for(8))

    def test_final_states_are_worded_and_frozen(self):
        r = rule(**GATES)
        dead = section_text(trades_db([-0.01] * 4 + [0.5] * 3), r, hours_for(7))
        assert 'DEAD, stopped at the 4-trade check' in dead and '| Trades counted | 4 of 8' in dead
        assert 'Later trades are not counted' in dead and 'no verdict until' not in dead
        alive = section_text(trades_db(WINNERS + [-0.9]), r, hours_for(9))
        assert 'ALIVE: the single test passed (8 trades in 2 weeks' in alive and 'not a trade signal' in alive
        assert 'execution stays disabled' in alive and '| Trades counted | 8 of 8 (100.0%) |' in alive
        no_edge = section_text(trades_db([0.03, -0.02, 0.025, -0.02, 0.03, -0.025, 0.02, -0.015]), r, hours_for(8))
        assert 'DEAD: no edge shown' in no_edge and 'one-sided p = ' in no_edge
        void = section_text(trades_db(WINNERS), rule(**GATES, code_sha256='0' * 64), hours_for(8))
        assert 'VOID: the pinned code' in void and 'restarted under a new name' in void and 'ALIVE' not in void

    def test_a_failing_description_does_not_cost_the_section(self, monkeypatch):
        conn = trades_db([0.01, 0.02])
        monkeypatch.setattr(forward_report, 'descriptions', lambda *a: 1 / 0)
        text = section_text(conn, rule(symbols=('BTCUSDT',)), 100)
        assert '| Trades counted | 2 of 600' in text and 'Descriptions unavailable this week' in text

    def test_a_failing_core_does_not_cost_the_rest_of_the_weekly_scorecard(self, monkeypatch):
        conn = trades_db([0.01, 0.02])
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('engine_start', ?)", (str(T0),))
        monkeypatch.setattr(settings, 'FROZEN_SIGNAL_RULES', (rule(symbols=('BTCUSDT',)),))
        monkeypatch.setattr(forward_rule, 'evaluate', lambda *a: 1 / 0)
        text = reports.weekly_report(conn, T0)
        assert 'could not be computed this week' in text and 'No count and no verdict' in text
        assert '## Rule v1b' in text and '## Forward evidence' in text

    def test_section_reads_only_and_leaves_the_current_symbol_alone(self, monkeypatch):
        conn = trades_db(WINNERS)
        monkeypatch.setattr(settings, 'SYMBOL', 'XRPUSDT')
        before = conn.total_changes
        section_text(conn, rule(**GATES), hours_for(8))
        assert conn.total_changes == before and settings.SYMBOL == 'XRPUSDT'


@pytest.mark.unit
class TestDescriptions:
    def _db(self):
        rng = random.Random(11)
        closes = {(c, k): 100.0 * (1 + rng.gauss(0, 0.01)) for c in COINS for k in range(400)}
        conn = make_db(400, closes=closes)
        # downtrend on ETH for the second half, mixed volume and taker-buy share on signal candles
        conn.execute("UPDATE indicators SET ema50=99 WHERE symbol='ETHUSDT' AND open_time>=?", (T0 + 200 * HOUR,))
        picks = [('BTCUSDT', 3, 'LR50'), ('BTCUSDT', 50, 'LR200'), ('ETHUSDT', 20, 'LR50'), ('ETHUSDT', 230, 'SR50'),
                 ('ETHUSDT', 300, 'SR200'), ('SOLUSDT', 100, 'LR50'), ('SOLUSDT', 250, 'LR50')]
        for coin, hour, name in picks:
            add_signal(conn, coin, hour, name)
        conn.execute("UPDATE candles SET volume=20, taker_buy_volume=12 WHERE symbol='BTCUSDT' AND open_time=?", (T0 + 3 * HOUR,))
        conn.execute("UPDATE candles SET volume=15, taker_buy_volume=7.5 WHERE symbol='SOLUSDT' AND open_time=?", (T0 + 100 * HOUR,))
        for coin in COINS:
            for h in range(0, 400, 8):
                add_funding(conn, coin, T0 + h * HOUR, rng.gauss(0.0001, 0.0001))
        return conn

    def _row(self, text, label):
        return next(line for line in text.splitlines() if line.startswith(f'| {label} |'))

    def test_splits_by_coin_direction_volume_and_buy_share(self):
        conn = self._db()
        e = ev(conn, rule(), 400)
        text = '\n'.join(forward_report.descriptions(conn, rule(), e))
        s = e['sample']
        assert len(s) == 7
        for label, group in (('BTCUSDT', [t for t in s if t['symbol'] == 'BTCUSDT']),
                             ('short', [t for t in s if t['direction'] == 'SHORT']),
                             ('long', [t for t in s if t['direction'] == 'LONG']),
                             ('volume 1.5 or more', [s[0], s[3]]),          # BTC hour 3 (2.0x) and SOL hour 100 (1.5x)
                             ('volume below 1.5', [s[1], s[2], s[4], s[5], s[6]]),
                             ('taker-buy share 0.5 or more', [s[0], s[3]]),  # 0.6 and exactly 0.5
                             ('taker-buy share below 0.5', [s[1], s[2], s[4], s[5], s[6]])):
            want = (f"| {label} | {len(group)} (too few) | {forward_report.bps(statistics.fmean(t['net'] for t in group))} | "
                    f"{forward_report.bps(statistics.fmean(t['gross'] for t in group))} | "
                    f"{sum(t['net'] > 0 for t in group)}/{len(group)} |")
            assert self._row(text, label) == want
        assert [(t['symbol'][:3], (t['signal_time'] - T0) // HOUR) for t in s] == [
            ('BTC', 3), ('ETH', 20), ('BTC', 50), ('SOL', 100), ('ETH', 230), ('SOL', 250), ('ETH', 300)]

    def _net(self, conn, coin, hour, sign):
        """A trade worked out by hand from the tables."""
        px = lambda col, k: conn.execute(f"SELECT {col} FROM candles WHERE symbol=? AND open_time=?",
                                         (coin, T0 + k * HOUR)).fetchone()[0]
        paid = conn.execute("SELECT COALESCE(SUM(rate), 0) FROM funding WHERE symbol=? AND funding_time>? AND funding_time<?",
                            (coin, T0 + (hour + 1) * HOUR, T0 + (hour + 13) * HOUR)).fetchone()[0]
        return sign * (px('close', hour + 12) / px('open', hour + 1) - 1) - COST - sign * paid

    def test_random_entry_excess_and_trend_only_control_match_a_hand_computation(self):
        conn = self._db()
        r = rule()
        e = ev(conn, r, 400)
        text = '\n'.join(forward_report.descriptions(conn, r, e))
        last_entry = 400 - 13                                            # latest signal hour whose trade has exited
        excess, refs = [], []
        for t in e['sample']:
            hour, sign = (t['signal_time'] - T0) // HOUR, 1 if t['direction'] == 'LONG' else -1
            assert t['net'] == pytest.approx(self._net(conn, t['symbol'], hour, sign))
            window = [self._net(conn, t['symbol'], k, sign) for k in range(hour + 1, min(hour + 720, last_entry) + 1)]
            if len(window) >= 24:
                refs.append(statistics.fmean(window))
                excess.append(t['net'] - refs[-1])
        assert len(excess) == 7
        assert f"- trades compared: 7 of 7" in text
        assert f"- average random entry: {forward_report.bps(statistics.fmean(refs))}" in text
        assert f"- average excess of the rule's trades over them: {forward_report.bps(statistics.fmean(excess))}" in text

        end = (e['sample'][-1]['exit_close'] - T0) // HOUR               # the control stops at the last trade's exit
        up = lambda coin, k: not (coin == 'ETHUSDT' and k >= 200)
        control = 0.0
        for t in e['sample']:
            sign = 1 if t['direction'] == 'LONG' else -1
            cell = [self._net(conn, t['symbol'], k, sign) for k in range(0, end - 13 + 1) if up(t['symbol'], k) == (sign == 1)]
            control += statistics.fmean(cell)
        control /= 7
        mean = statistics.fmean(t['net'] for t in e['sample'])
        assert f"- rule's trades: {forward_report.bps(mean)} (7 trades)" in text
        assert f"- trend-only entries: {forward_report.bps(control)}" in text
        assert f"- difference: {forward_report.bps(mean - control)}" in text

    def test_maker_fee_line_changes_only_the_fee(self):
        conn = self._db()
        e = ev(conn, rule(), 400)
        text = '\n'.join(forward_report.descriptions(conn, rule(), e))
        maker = statistics.fmean(t['net'] for t in e['sample']) + COST - 0.0004
        assert f"with a 4 bps round trip (maker fees, no slippage) instead of 14 bps the mean net would be {forward_report.bps(maker)}" in text
        assert 'not a model of getting filled' in text

    def test_descriptions_do_not_change_the_verdict(self, monkeypatch):
        conn = trades_db(WINNERS)
        r = rule(**GATES)
        e = ev(conn, r, hours_for(8))
        snapshot = (e['state'], dict(e['test']), [t['net'] for t in e['sample']])
        forward_report.descriptions(conn, r, e)
        assert (e['state'], e['test'], [t['net'] for t in e['sample']]) == snapshot
        monkeypatch.setattr(forward_report, 'descriptions', lambda *a: ['x'])
        assert 'ALIVE: the single test passed' in section_text(conn, r, hours_for(8))

    def test_no_trades_yet(self):
        conn = make_db(30)
        assert 'No counted trades yet.' in '\n'.join(forward_report.descriptions(conn, rule(), ev(conn, rule(), 30)))


@pytest.mark.unit
def test_forward_test_code_has_no_orders_keys_or_network():
    for name in ('forward_rule.py', 'forward_report.py'):
        src = (forward_rule.PACKAGE_DIR / name).read_text()
        for word in ('import trading', 'from trading', 'urllib', 'requests', 'api_key', 'API_KEY', 'secret', 'INSERT', 'UPDATE', 'DELETE'):
            assert word not in src, (name, word)
