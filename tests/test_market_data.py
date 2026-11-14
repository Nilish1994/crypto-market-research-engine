"""
Market-data recorder: TradingView-equivalent indicators, Strategy-1 rules,
closed-candle handling, no look-ahead in context, outcomes, and
incremental-update equals full-rebuild.
"""
import random
from pathlib import Path

import pytest

from market_data import binance_futures, fair_test, pipeline, settings, store, strategy1
from market_data.indicators import IndicatorEngine, ema, rma, rsi_from_averages, trend

HOUR = settings.TIMEFRAMES['1h']


# ── Reference implementations written straight from the Pine definitions ──

def ref_sma(xs, n):
    return [None if i < n - 1 else sum(xs[i - n + 1:i + 1]) / n for i in range(len(xs))]


def ref_ema(xs, n, alpha=None):
    alpha = alpha if alpha is not None else 2 / (n + 1)
    out, prev = [], None
    for i, x in enumerate(xs):
        if prev is None:
            prev = sum(xs[:n]) / n if i == n - 1 else None
        else:
            prev = alpha * x + (1 - alpha) * prev
        out.append(prev)
    return out


def ref_rsi(closes, n):
    ups = [max(closes[i] - closes[i - 1], 0) for i in range(1, len(closes))]
    downs = [max(closes[i - 1] - closes[i], 0) for i in range(1, len(closes))]
    ag, al = ref_ema(ups, n, 1 / n), ref_ema(downs, n, 1 / n)
    return [None] + [rsi_from_averages(g, l) for g, l in zip(ag, al)]


def make_candles(n, start_open=0, dur=HOUR, seed=1, price=30000.0):
    rng = random.Random(seed)
    out = []
    for i in range(n):
        o = price
        c = max(100.0, o * (1 + rng.gauss(0, 0.004)))
        h = max(o, c) * (1 + abs(rng.gauss(0, 0.002)))
        l = min(o, c) * (1 - abs(rng.gauss(0, 0.002)))
        out.append({'open_time': start_open + i * dur, 'close_time': start_open + (i + 1) * dur - 1,
                    'open': o, 'high': h, 'low': l, 'close': c, 'volume': rng.uniform(100, 1000),
                    'quote_volume': 0.0, 'trades': 1, 'taker_buy_volume': 0.0, 'taker_buy_quote_volume': 0.0})
        price = c
    return out


def as_kline(c):
    return [c['open_time'], str(c['open']), str(c['high']), str(c['low']), str(c['close']), str(c['volume']),
            c['close_time'], '0', 1, '0', '0', '0']


def fake_binance(series: dict):
    """series: timeframe -> candle dicts. Mimics /fapi/v1/klines paging."""
    def get_json(params):
        rows = [c for c in series[params['interval']] if c['open_time'] >= params['startTime']]
        return [as_kline(c) for c in rows[:params['limit']]]
    return get_json


REAL_FETCH_FUNDING = binance_futures.fetch_funding


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(settings, 'REQUEST_PAUSE_SEC', 0)
    # sync() also downloads funding; tests that need it pass their own fake
    monkeypatch.setattr(binance_futures, 'fetch_funding', lambda start, get_json=None: iter(()))


@pytest.fixture
def conn():
    c = store.connect(':memory:')
    yield c
    c.close()


def dump(conn, table):
    return [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2, 3, 4, 5")]


# ── Indicators ──

@pytest.mark.unit
class TestIndicators:
    def test_ema_matches_pine_definition(self):
        closes = [c['close'] for c in make_candles(400)]
        for n in (50, 200):
            e = ema(n)
            got = [e.step(x) for x in closes]
            assert got[:n - 1] == [None] * (n - 1) and got[n - 1] == pytest.approx(sum(closes[:n]) / n)
            assert got[-1] == pytest.approx(ref_ema(closes, n)[-1], rel=1e-12)

    def test_rma_uses_wilder_alpha(self):
        r = rma(3)
        assert [r.step(x) for x in (1, 2, 3, 6)] == [None, None, 2.0, pytest.approx(2 + (6 - 2) / 3)]

    def test_engine_matches_reference_rsi_ema_sma(self):
        candles = make_candles(300)
        eng = IndicatorEngine()
        rows = [eng.step(c) for c in candles]
        closes = [c['close'] for c in candles]
        vols = [c['volume'] for c in candles]
        rsi, e50, e200, vsma = ref_rsi(closes, 14), ref_ema(closes, 50), ref_ema(closes, 200), ref_sma(vols, 20)
        for i, row in enumerate(rows):
            for key, ref in (('rsi14', rsi), ('ema50', e50), ('ema200', e200), ('volume_sma20', vsma)):
                if ref[i] is None:
                    assert row[key] is None, (key, i)
                else:
                    assert row[key] == pytest.approx(ref[i], rel=1e-9), (key, i)
        assert rows[13]['rsi14'] is None and rows[14]['rsi14'] is not None   # first RSI at bar 14

    def test_rsi_zero_division_rules(self):
        assert rsi_from_averages(1.0, 0.0) == 100.0
        assert rsi_from_averages(0.0, 1.0) == 0.0
        assert rsi_from_averages(1.0, 1.0) == 50.0
        assert rsi_from_averages(None, 1.0) is None

    def test_trend(self):
        assert trend(2, 1) == 'UP' and trend(1, 2) == 'DOWN'
        assert trend(None, 1) is None and trend(1, 1) is None


# ── Strategy-1 rules ──

def candle(o, h, l, c):
    return {'open': o, 'high': h, 'low': l, 'close': c}


@pytest.mark.unit
class TestStrategy1:
    # EMA50 = 100, EMA200 = 90 → up trend; zone around EMA50 = 99.85 .. 100.15
    def test_long_retest_ema50(self):
        assert strategy1.signals_for(candle(100.2, 101, 99.9, 100.5), 100, 90) == ['LR50']

    def test_low_on_zone_edges_counts(self):
        assert strategy1.signals_for(candle(100.2, 101, 100 * 0.9985, 100.5), 100, 90) == ['LR50']
        assert strategy1.signals_for(candle(100.2, 101, 100 * 1.0015, 100.5), 100, 90) == ['LR50']

    def test_wick_too_deep_or_too_shallow_is_no_signal(self):
        assert strategy1.signals_for(candle(100.2, 101, 99.80, 100.5), 100, 90) == []
        assert strategy1.signals_for(candle(100.4, 101, 100.20, 100.5), 100, 90) == []

    def test_long_needs_green_candle_and_close_above_ema(self):
        assert strategy1.signals_for(candle(100.6, 101, 99.9, 100.5), 100, 90) == []      # red
        assert strategy1.signals_for(candle(99.88, 100, 99.86, 99.95), 100, 90) == []     # close below EMA50

    def test_long_retest_ema200(self):
        assert strategy1.signals_for(candle(90.1, 91, 89.95, 90.5), 100, 90) == ['LR200']

    def test_short_retests(self):
        # EMA50 = 90, EMA200 = 100 → down trend
        assert strategy1.signals_for(candle(89.8, 90.05, 89, 89.5), 90, 100) == ['SR50']
        assert strategy1.signals_for(candle(99.8, 100.1, 99, 99.5), 90, 100) == ['SR200']
        assert strategy1.signals_for(candle(89.4, 90.05, 89, 89.5), 90, 100) == []        # green candle

    def test_both_emas_can_fire_on_one_candle(self):
        assert strategy1.signals_for(candle(100.2, 101, 99.95, 100.5), 100.05, 99.95) == ['LR50', 'LR200']

    def test_no_signal_until_emas_exist(self):
        assert strategy1.signals_for(candle(100.2, 101, 99.9, 100.5), 100, None) == []
        assert strategy1.signals_for(candle(100.2, 101, 99.9, 100.5), None, 90) == []

    def test_no_signal_when_emas_equal(self):
        assert strategy1.signals_for(candle(100.2, 101, 99.9, 100.5), 100, 100) == []


# ── Fetching ──

@pytest.mark.unit
def test_forming_candle_is_never_stored(conn):
    candles = make_candles(10)
    now = candles[-1]['open_time'] + 1000          # last candle still forming
    pipeline.fetch_candles(conn, ['1h'], now_ms=now, get_json=fake_binance({'1h': candles}))
    assert store.last_open_time(conn, 'candles', settings.SYMBOL, '1h') == candles[-2]['open_time']


@pytest.mark.unit
def test_paging_fetches_everything(conn, monkeypatch):
    monkeypatch.setattr(settings, 'KLINES_LIMIT', 7)
    candles = make_candles(30)
    added = pipeline.fetch_candles(conn, ['1h'], now_ms=10 ** 15, get_json=fake_binance({'1h': candles}))
    assert added == {'1h': 30}


# ── Whole pipeline ──

def series_for_all(n_hours=600):
    """Each timeframe gets its own deterministic random walk covering the same period."""
    span = n_hours * HOUR
    return {tf: make_candles(span // dur, dur=dur, seed=i + 1) for i, (tf, dur) in enumerate(settings.TIMEFRAMES.items())}


@pytest.mark.unit
def test_incremental_updates_equal_full_rebuild():
    series = series_for_all()
    get_json = fake_binance(series)
    end = 600 * HOUR

    full = store.connect(':memory:')
    pipeline.sync(full, now_ms=end, get_json=get_json)

    inc = store.connect(':memory:')
    for cut in (130 * HOUR, 140 * HOUR, 333 * HOUR + 7, end):    # first call is before EMA200 warms up on 1h
        pipeline.sync(inc, now_ms=cut, get_json=get_json)

    for table in ('candles', 'indicators', 'signals', 'signal_context', 'outcomes'):
        assert dump(inc, table) == dump(full, table), table
    assert dump(full, 'signals'), 'synthetic data should produce some signals'


@pytest.mark.unit
def test_rebuild_reproduces_the_same_tables():
    get_json = fake_binance(series_for_all())
    conn = store.connect(':memory:')
    pipeline.sync(conn, now_ms=600 * HOUR, get_json=get_json)
    before = {t: dump(conn, t) for t in pipeline.DERIVED_TABLES}
    pipeline.sync(conn, now_ms=600 * HOUR, get_json=get_json, rebuild=True)
    assert {t: dump(conn, t) for t in pipeline.DERIVED_TABLES} == before


@pytest.mark.unit
def test_context_uses_only_closed_higher_timeframe_candles(conn):
    four_h = settings.TIMEFRAMES['4h']
    store.insert_candles(conn, settings.SYMBOL, '4h', make_candles(3, dur=four_h))
    # 1h candle opening 10:00 closes 10:59:59.999; the 08:00 4h candle is still open then
    ctx = pipeline.latest_closed(conn, '4h', 10 * HOUR + HOUR - 1)
    assert ctx['open_time'] == 4 * HOUR
    # at 11:59:59.999 the 08:00 4h candle has closed
    assert pipeline.latest_closed(conn, '4h', 12 * HOUR - 1)['open_time'] == 8 * HOUR
    # a 5m candle closing at the same instant is known
    store.insert_candles(conn, settings.SYMBOL, '5m', make_candles(200, dur=settings.TIMEFRAMES['5m']))
    assert pipeline.latest_closed(conn, '5m', 11 * HOUR - 1)['open_time'] == 11 * HOUR - settings.TIMEFRAMES['5m']


@pytest.mark.unit
class TestOutcomes:
    WINDOW = [{'open': 101, 'high': 103, 'low': 99, 'close': 102},
              {'open': 102, 'high': 104, 'low': 98, 'close': 97}]

    def test_long(self):
        out = pipeline.measure_outcome('LONG', 100, self.WINDOW)
        assert out == {'next_open': 101, 'max_favourable_pct': pytest.approx(4), 'max_adverse_pct': pytest.approx(2),
                       'close_change_pct': pytest.approx(-3)}

    def test_short_is_mirrored(self):
        out = pipeline.measure_outcome('SHORT', 100, self.WINDOW)
        assert out['max_favourable_pct'] == pytest.approx(2)
        assert out['max_adverse_pct'] == pytest.approx(4)
        assert out['close_change_pct'] == pytest.approx(3)

    def test_gap_or_missing_candles_leave_outcome_pending(self, conn):
        candles = make_candles(20)
        del candles[12]                                            # Binance gap
        store.insert_candles(conn, settings.SYMBOL, '1h', candles)
        conn.execute("INSERT INTO signals VALUES (?, '1h', ?, 'LR50', 'LONG', 1, 1)", (settings.SYMBOL, 5 * HOUR))
        conn.execute("INSERT INTO signals VALUES (?, '1h', ?, 'LR50', 'LONG', 1, 1)", (settings.SYMBOL, 0))
        pipeline.update_outcomes(conn, '1h')
        got = {(r[0], r[1]) for r in conn.execute("SELECT open_time, horizon FROM outcomes")}
        # signal at 0: horizons 3, 6 fine; 12 would cross the gap. Signal at 5h: 3, 6 fine (5+6=11 < 12); 12 crosses gap.
        assert got == {(0, 3), (0, 6), (5 * HOUR, 3), (5 * HOUR, 6)}


@pytest.mark.unit
def test_recorder_is_independent_of_the_trading_bot():
    src = ''.join(p.read_text() for p in Path(pipeline.__file__).parent.glob('*.py'))
    assert 'import trading' not in src and 'from trading' not in src
    assert settings.DB_PATH.name == 'market_data.db'


# ── Funding and the fair test ──

def fake_funding(rows):
    def get_json(params):
        page = [r for r in rows if r[0] >= params['startTime']][:params['limit']]
        return [{'fundingTime': t, 'fundingRate': str(r)} for t, r in page]
    return get_json


@pytest.mark.unit
def test_funding_is_paged_and_resumed(conn, monkeypatch):
    monkeypatch.setattr(binance_futures, 'fetch_funding', REAL_FETCH_FUNDING)
    monkeypatch.setattr(settings, 'FUNDING_LIMIT', 4)
    eight_h = 8 * HOUR
    rows = [(settings.FUTURES_LISTING_MS + k * eight_h, 0.0001 * k) for k in range(10)]
    assert pipeline.fetch_funding(conn, get_json=fake_funding(rows[:7])) == 7
    assert pipeline.fetch_funding(conn, get_json=fake_funding(rows)) == 3
    assert pipeline.fetch_funding(conn, get_json=fake_funding(rows)) == 0


@pytest.mark.unit
class TestFairTest:
    CANDLES = [{'open_time': k * HOUR, 'open': o, 'high': h, 'low': l, 'close': c}
               for k, (o, h, l, c) in enumerate([(100, 100, 100, 100),     # signal candle
                                                 (100, 103, 98, 102),
                                                 (102, 106, 101, 105),
                                                 (105, 105, 104, 104)])]
    FUNDING = fair_test.FundingLedger([(HOUR + 10, 0.001), (2 * HOUR + 10, 0.002), (5 * HOUR, 0.5)])

    def test_funding_ledger_sums_a_range(self):
        assert self.FUNDING.paid_by_long(HOUR, 3 * HOUR) == pytest.approx(0.003)
        assert self.FUNDING.paid_by_long(HOUR + 10, 3 * HOUR) == pytest.approx(0.002)   # settlement at t_from excluded

    def test_long_enters_next_open_and_pays_fee_and_funding(self):
        t = fair_test.trade_result(self.CANDLES, 0, 2, 'LONG', 0.001, self.FUNDING, HOUR)
        assert t['gross'] == pytest.approx(0.05)                 # 100 → 105
        assert t['funding'] == pytest.approx(0.003)
        assert t['net'] == pytest.approx(0.05 - 0.001 - 0.003)
        assert t['worst'] == pytest.approx(-0.02)

    def test_short_is_mirrored_and_receives_positive_funding(self):
        t = fair_test.trade_result(self.CANDLES, 0, 2, 'SHORT', 0.001, self.FUNDING, HOUR)
        assert t['gross'] == pytest.approx(-0.05)
        assert t['funding'] == pytest.approx(-0.003)
        assert t['net'] == pytest.approx(-0.05 - 0.001 + 0.003)
        assert t['worst'] == pytest.approx(-0.06)

    def test_no_trade_without_the_exit_candle(self):
        assert fair_test.trade_result(self.CANDLES, 1, 3, 'LONG', 0, self.FUNDING, HOUR) is None

    def test_signals_during_an_open_trade_are_skipped(self):
        candles = make_candles(30)
        trades = fair_test.non_overlapping(candles, [0, 1, 2, 3, 4, 10], 3, 'LONG', 0, fair_test.FundingLedger([]), HOUR)
        assert [t['open_time'] // HOUR for t in trades] == [0, 4, 10]

    def test_report_runs_on_a_full_pipeline(self):
        conn = store.connect(':memory:')
        pipeline.sync(conn, now_ms=600 * HOUR, get_json=fake_binance(series_for_all()))
        sig = conn.execute("SELECT timeframe, signal FROM signals GROUP BY 1, 2 ORDER BY COUNT(*) DESC").fetchone()
        text = fair_test.run(conn, sig[0], sig[1], holds=(3, 6), runs=50)
        assert f"Fair test: {sig[1]}" in text and 'no funding history' in text
        assert 'Beats random in' in text and 'Baseline: every trend candle' in text


@pytest.mark.unit
def test_two_coins_in_one_database_stay_separate(monkeypatch):
    btc, eth = series_for_all(300), {tf: [dict(c, close=c['close'] / 20, open=c['open'] / 20, high=c['high'] / 20,
                                                 low=c['low'] / 20) for c in rows]
                                     for tf, rows in series_for_all(300).items()}
    alone = store.connect(':memory:')
    monkeypatch.setattr(settings, 'SYMBOL', 'ETHUSDT')
    pipeline.sync(alone, now_ms=300 * HOUR, get_json=fake_binance(eth))

    shared = store.connect(':memory:')
    monkeypatch.setattr(settings, 'SYMBOL', 'BTCUSDT')
    pipeline.sync(shared, now_ms=300 * HOUR, get_json=fake_binance(btc))
    monkeypatch.setattr(settings, 'SYMBOL', 'ETHUSDT')
    pipeline.sync(shared, now_ms=300 * HOUR, get_json=fake_binance(eth))
    pipeline.sync(shared, now_ms=300 * HOUR, get_json=fake_binance(eth), rebuild=True)   # rebuild touches ETH only

    for table in ('candles', 'indicators', 'signals', 'signal_context', 'outcomes'):
        eth_rows = [r for r in dump(shared, table) if r[0] == 'ETHUSDT']
        assert eth_rows == dump(alone, table), table
        assert any(r[0] == 'BTCUSDT' for r in dump(shared, table)) or table != 'candles'


# ── Research engine: levels, visits, reports, watch cycle ──

from market_data import levels, reports, watch   # noqa: E402

H4 = settings.TIMEFRAMES['4h']


def bar(i, o, h, l, c):
    return {'open_time': i * H4, 'open': o, 'high': h, 'low': l, 'close': c}


def below_then(rest, n_before=7):
    """n_before calm candles at 95 (well under a level at 100), then the given (o, h, l, c) candles."""
    rows = [(95, 96, 94, 95)] * n_before + rest
    return [bar(i, *r) for i, r in enumerate(rows)]


@pytest.fixture
def from_2020_off(monkeypatch):
    monkeypatch.setattr(settings, 'HISTORY_START_MS', 0)       # synthetic candles start in 1970


@pytest.mark.unit
class TestLevels:
    def test_swing_is_known_only_after_three_more_candles_close(self):
        highs = [1, 2, 3, 9, 3, 2, 1, 1, 1]
        cs = [{'open_time': i * H4, 'high': h, 'low': 0.5 - i * 0.01} for i, h in enumerate(highs)]
        found = [x for x in levels.find_swings(cs, '4h') if x['kind'] == 'H']
        assert [(x['formed_at'], x['known_at'], x['price']) for x in found] == [(3 * H4, 7 * H4, 9)]

    def test_equal_highs_are_not_a_swing(self):
        cs = [{'open_time': i * H4, 'high': h, 'low': 0} for i, h in enumerate([1, 2, 9, 9, 2, 1, 1, 1])]
        assert not [x for x in levels.find_swings(cs, '4h') if x['kind'] == 'H']

    def test_nearby_level_is_merged_until_the_first_expires(self):
        lv = lambda price, known: {'price': price, 'known_at': known, 'expires_at': known + 10, 'formed_at': known, 'kind': 'L'}
        kept = levels.merge_levels([lv(100, 0), lv(100.2, 5), lv(100.2, 12), lv(105, 6)], pct=0.003)
        assert [(x['price'], x['known_at']) for x in kept] == [(100, 0), (105, 6), (100.2, 12)]


@pytest.mark.unit
class TestVisits:
    LEVEL = staticmethod(lambda i: 100.0)

    def test_failed_push_into_resistance_is_held_with_bounce_measured(self):
        cs = below_then([(95, 99.9, 95, 98)] + [(98, 99, 96, 97)] * 5 + [(97, 97, 90, 92)] * 30)
        (v,) = levels.compute_visits(cs, self.LEVEL)
        assert (v['side'], v['result'], v['tests'], v['touch_i'], v['decided_i']) == ('resistance', 'held', 1, 7, 12)
        assert v['away'][6] == pytest.approx(1 - cs[18]['close'] / cs[13]['open'])   # down from resistance = positive

    def test_close_through_the_zone_is_a_break_and_a_return_is_a_fakeout(self):
        cs = below_then([(95, 99.9, 95, 98), (98, 102, 98, 101)] + [(98, 99, 96, 98)] * 30)
        (v,) = levels.compute_visits(cs, self.LEVEL)
        assert (v['result'], v['break_i'], v['fakeout'], v['tests']) == ('broke', 8, 1, 2)

    def test_break_that_stays_beyond_is_not_a_fakeout(self):
        cs = below_then([(95, 99.9, 95, 98), (98, 102, 98, 101)] + [(103, 105, 102, 104)] * 30)
        assert levels.compute_visits(cs, self.LEVEL)[0]['fakeout'] == 0

    def test_open_window_is_pending(self):
        cs = below_then([(95, 99.9, 95, 98), (98, 99, 96, 97)])
        (v,) = levels.compute_visits(cs, self.LEVEL)
        assert v['result'] is None and v['away'] == {}

    def test_support_from_above_is_mirrored(self):
        rows = [(105, 106, 104, 105)] * 7 + [(105, 105, 100.1, 102)] + [(102, 104, 101, 103)] * 5 + [(103, 110, 103, 108)] * 30
        (v,) = levels.compute_visits([bar(i, *r) for i, r in enumerate(rows)], self.LEVEL)
        assert (v['side'], v['result']) == ('support', 'held') and v['away'][6] > 0

    def test_previous_candle_inside_the_zone_is_not_an_approach(self):
        cs = below_then([(95, 99.9, 95, 98), (98, 99.95, 97, 98)] + [(98, 99, 96, 97)] * 30, n_before=7)
        assert [v['touch_i'] for v in levels.compute_visits(cs, self.LEVEL)] == [7]   # candle 8 follows a touch

    def test_only_five_closes_on_one_side_is_not_an_approach(self):
        rows = [(101, 102, 100.5, 101)] + [(95, 96, 94, 95)] * 5 + [(95, 99.9, 95, 98)] + [(98, 99, 96, 97)] * 30
        cs = [bar(i, *r) for i, r in enumerate(rows)]
        assert levels.compute_visits(cs, self.LEVEL, lo=6, hi=7) == []

    def test_break_that_carries_on_has_negative_away_moves_up_to_24(self):
        cs = below_then([(95, 99.9, 95, 98), (98, 102, 98, 101)] + [(101 + k, 103 + k, 100 + k, 102 + k) for k in range(30)])
        v = levels.compute_visits(cs, self.LEVEL)[0]
        assert v['result'] == 'broke' and set(v['away']) == {6, 12, 24} and all(x < 0 for x in v['away'].values())

    def test_no_visit_before_the_level_is_known(self):
        cs = below_then([(95, 99.9, 95, 98)] + [(98, 99, 96, 97)] * 30)
        assert levels.compute_visits(cs, lambda i: 100.0 if i >= 8 else None) == []


def engine_db(monkeypatch, n_hours=900):
    monkeypatch.setattr(settings, 'SYMBOL', 'BTCUSDT')
    conn = store.connect(':memory:')
    pipeline.sync(conn, now_ms=n_hours * HOUR, get_json=fake_binance(series_for_all(n_hours)))
    return conn


@pytest.mark.unit
class TestVisitStore:
    def test_manual_level_visits_start_after_it_was_added(self, monkeypatch, from_2020_off):
        conn = engine_db(monkeypatch)
        c4 = conn.execute("SELECT open_time, close FROM candles WHERE timeframe='4h' ORDER BY open_time").fetchall()
        known = c4[len(c4) // 3]['open_time']
        price = statistics_median([r['close'] for r in c4])
        levels.add_manual_level(conn, price, 'test', at=known)
        levels.update_visits(conn, now=1)
        times = [r[0] for r in conn.execute("SELECT touch_time FROM level_visits WHERE source='manual'")]
        assert times and min(times) >= known

    def test_ema_level_uses_the_value_known_at_the_candle_open(self, monkeypatch, from_2020_off):
        conn = engine_db(monkeypatch)
        levels.update_visits(conn, now=1)
        rows = conn.execute("SELECT touch_time, level_price FROM level_visits WHERE source='ema50_4h'").fetchall()
        assert rows
        for t, price in rows:
            prev = conn.execute("SELECT ema50 FROM indicators WHERE timeframe='4h' AND open_time=?", (t - H4,)).fetchone()[0]
            assert price == pytest.approx(prev)

    def test_complete_rows_are_never_rewritten(self, monkeypatch, from_2020_off):
        conn = engine_db(monkeypatch)
        levels.update_levels(conn, now=1)
        levels.update_visits(conn, now=1)
        done = conn.execute("SELECT level_id, touch_time FROM level_visits WHERE complete=1 LIMIT 1").fetchone()
        assert done is not None
        conn.execute("UPDATE level_visits SET away_12=999 WHERE level_id=? AND touch_time=?", tuple(done))
        conn.execute("UPDATE level_visits SET away_6=999, complete=0 WHERE complete=1 AND NOT (level_id=? AND touch_time=?)",
                     tuple(done))
        levels.update_visits(conn, now=2)
        assert tuple(conn.execute("SELECT away_12, first_logged_at FROM level_visits WHERE level_id=? AND touch_time=?",
                                  tuple(done)).fetchone()) == (999, 1)
        assert conn.execute("SELECT COUNT(*) FROM level_visits WHERE away_6=999").fetchone()[0] == 0   # pending rows refreshed
        assert conn.execute("SELECT MIN(first_logged_at) FROM level_visits").fetchone()[0] == 1          # first log time kept


def statistics_median(xs):
    xs = sorted(xs)
    return xs[len(xs) // 2]


@pytest.mark.unit
class TestReportsAndCycle:
    def test_due_reports_wait_until_after_midnight_and_skip_written_files(self, tmp_path):
        at = lambda s: to_ms_utc(s)
        due = reports.due_reports(at('2026-11-02 00:10'), tmp_path)
        assert [(k, p.name) for k, _, p in due] == [('daily', '2026-11-01.md'), ('weekly', '2026-W44.md')]
        assert [p.name for _, _, p in reports.due_reports(at('2026-11-02 00:03'), tmp_path)] == ['2026-10-31.md', '2026-W43.md']
        (tmp_path / 'daily').mkdir()
        (tmp_path / 'daily' / '2026-11-01.md').write_text('x')
        assert [k for k, _, _ in reports.due_reports(at('2026-11-02 00:10'), tmp_path)] == ['weekly']

    def test_cycle_watches_each_coin_writes_reports_and_skips_work_without_a_new_4h_candle(self, monkeypatch, tmp_path,
                                                                                          from_2020_off):
        conn = store.connect(':memory:')
        get_json = fake_binance(series_for_all(900))
        now = 900 * HOUR
        first = watch.run_cycle(conn, symbols=['BTCUSDT', 'ETHUSDT'], now=now, get_json=get_json, report_dir=tmp_path)
        assert first['BTCUSDT']['visits']['visits'] > 0 and first['ETHUSDT']['candles_added'] > 0
        assert len(first['reports_written']) == 2
        assert conn.execute("SELECT value FROM meta WHERE key='engine_start'").fetchone()[0] == str(now)
        daily = next(p for p in first['reports_written'] if '/daily/' in p)
        text = Path(daily).read_text()
        assert '## BTCUSDT' in text and '## ETHUSDT' in text and 'not trade signals' in text
        weekly = Path(next(p for p in first['reports_written'] if '/weekly/' in p)).read_text()
        assert 'Forward evidence' in weekly and 'random flat lines' in weekly and 'EMA-shaped' in weekly
        assert ' UTC by engine version ' in weekly.splitlines()[-1]

        second = watch.run_cycle(conn, symbols=['BTCUSDT', 'ETHUSDT'], now=now + 60_000, get_json=get_json, report_dir=tmp_path)
        assert 'visits' not in second['BTCUSDT'] and second['reports_written'] == []
        assert settings.SYMBOL == 'BTCUSDT'                    # restored after the cycle

    def test_one_coin_failing_does_not_stop_the_others(self, monkeypatch, tmp_path, from_2020_off):
        good = fake_binance(series_for_all(300))

        def flaky(params):
            if params['symbol'] == 'ETHUSDT':
                raise OSError('exchange down')
            return good(params)
        conn = store.connect(':memory:')
        out = watch.run_cycle(conn, symbols=['ETHUSDT', 'BTCUSDT'], now=300 * HOUR, get_json=flaky, report_dir=tmp_path)
        assert 'sync_error' in out['ETHUSDT'] and out['BTCUSDT']['candles_added'] > 0

    def test_second_cycle_is_skipped_while_one_is_running(self, tmp_path):
        import fcntl
        db = tmp_path / 'm.db'
        with open(str(db) + '.watch.lock', 'w') as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            assert watch.run_locked(db) == {'skipped': 'another cycle or a manual write is running'}


def to_ms_utc(text):
    from datetime import datetime, timezone
    return int(datetime.strptime(text, '%Y-%m-%d %H:%M').replace(tzinfo=timezone.utc).timestamp() * 1000)


DAY = 86_400_000


def put(conn, tf, open_time, o, h, l, c, ema50=None, ema200=None):
    dur = settings.TIMEFRAMES[tf]
    store.insert_candles(conn, settings.SYMBOL, tf, [{
        'open_time': open_time, 'close_time': open_time + dur - 1, 'open': o, 'high': h, 'low': l, 'close': c,
        'volume': 1.0, 'quote_volume': 0.0, 'trades': 1, 'taker_buy_volume': 0.5, 'taker_buy_quote_volume': 0.0}])
    conn.execute("INSERT INTO indicators VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (settings.SYMBOL, tf, open_time, ema50, ema200, None, None, None, None, 1))


@pytest.mark.unit
class TestNoLookAheadInContext:
    """A touch at 08:00 on day 2 must see day 1's daily EMA and trend, and only funding settled by its close."""
    D0 = 10 * DAY

    def build(self, monkeypatch):
        monkeypatch.setattr(settings, 'SYMBOL', 'BTCUSDT')
        conn = store.connect(':memory:')
        for d in range(12):                                   # day 1: EMA50 100 > EMA200 90; from day 2: 200 < 300
            e50, e200 = (100.0, 90.0) if d < 2 else (200.0, 300.0)
            put(conn, '1d', self.D0 + d * DAY, 95, 96, 94, 95, e50, e200)
        for i in range(50):                                   # candle 14 = day 2 08:00 pushes into 100 and fails
            row = (95, 99.95, 95, 98) if i == 14 else (95, 96, 94, 95)
            put(conn, '4h', self.D0 + i * H4, *row)
        close_14 = self.D0 + 15 * H4
        conn.executemany("INSERT INTO funding VALUES ('BTCUSDT', ?, ?)",
                         [(close_14 - HOUR, 0.0001), (close_14 + 10, 0.9)])
        conn.commit()
        return conn

    def test_daily_ema_trend_and_funding_come_from_closed_data(self, monkeypatch, from_2020_off):
        conn = self.build(monkeypatch)
        levels.update_visits(conn, now=1)
        rows = conn.execute("SELECT touch_time, level_price, trend_1d, funding_rate FROM level_visits "
                            "WHERE source='ema50_1d'").fetchall()
        assert [tuple(r) for r in rows] == [(self.D0 + 14 * H4, 100.0, 'UP', 0.0001)]


@pytest.mark.unit
class TestEngineSafety:
    def test_rebuild_keeps_levels_and_visits(self, monkeypatch, from_2020_off):
        conn = engine_db(monkeypatch)
        levels.update_levels(conn, now=1)
        levels.update_visits(conn, now=1)
        before = [dump(conn, 'levels'), dump(conn, 'level_visits')]
        pipeline.sync(conn, now_ms=900 * HOUR, get_json=fake_binance(series_for_all(900)), rebuild=True)
        assert [dump(conn, 'levels'), dump(conn, 'level_visits')] == before

    def test_unfinished_rows_no_longer_produced_are_removed_but_complete_ones_stay(self, monkeypatch, from_2020_off):
        conn = engine_db(monkeypatch)
        cols = ', '.join(levels.VISIT_COLUMNS)
        marks = ', '.join('?' * len(levels.VISIT_COLUMNS))
        ghost = lambda lid, complete: tuple({'symbol': 'BTCUSDT', 'level_id': lid, 'touch_time': 5, 'source': 'x',
                                             'level_price': 1, 'side': 'support', 'tests': 1, 'complete': complete,
                                             'first_logged_at': 1, 'updated_at': 1}.get(k) for k in levels.VISIT_COLUMNS)
        conn.executemany(f"INSERT INTO level_visits ({cols}) VALUES ({marks})", [ghost('ghost', 0), ghost('kept', 1)])
        out = levels.update_visits(conn, now=2)
        ids = {r[0] for r in conn.execute("SELECT level_id FROM level_visits WHERE touch_time=5")}
        assert ids == {'kept'} and out['stale_removed'] == 1

    def test_rows_and_meta_carry_the_code_version(self, monkeypatch, tmp_path, from_2020_off):
        conn = store.connect(':memory:')
        watch.run_cycle(conn, symbols=['BTCUSDT'], now=900 * HOUR, get_json=fake_binance(series_for_all(900)),
                        report_dir=tmp_path, version='abc123')
        assert {r[0] for r in conn.execute("SELECT code_version FROM level_visits")} == {'abc123'}
        assert {r[0] for r in conn.execute("SELECT code_version FROM levels")} == {'abc123'}
        assert conn.execute("SELECT value FROM meta WHERE key='code_version'").fetchone()[0] == 'abc123'
        assert conn.execute("SELECT value FROM meta WHERE key='sync_status:BTCUSDT'").fetchone()[0].startswith('ok ')

    def test_code_version_is_a_git_commit(self):
        v = watch.code_version()
        assert v == 'unknown' or len(v.split('-')[0]) == 12

    def test_level_failure_for_one_coin_does_not_stop_the_others(self, monkeypatch, tmp_path, from_2020_off):
        real = levels.update_visits

        def flaky(conn, now=None, code_version=None):
            if settings.SYMBOL == 'ETHUSDT':
                raise ValueError('boom')
            return real(conn, now, code_version)
        monkeypatch.setattr(levels, 'update_visits', flaky)
        conn = store.connect(':memory:')
        out = watch.run_cycle(conn, symbols=['ETHUSDT', 'BTCUSDT'], now=900 * HOUR,
                              get_json=fake_binance(series_for_all(900)), report_dir=tmp_path, version='v')
        assert 'levels_error' in out['ETHUSDT'] and out['BTCUSDT']['visits']['visits'] > 0
        assert len(out['reports_written']) == 2
        assert conn.execute("SELECT value FROM meta WHERE key='visits_through:ETHUSDT'").fetchone() is None   # retried next cycle

    def test_add_level_waits_for_the_engine_lock(self, monkeypatch, tmp_path):
        from contextlib import contextmanager
        from market_data.__main__ import main
        calls = []

        @contextmanager
        def fake_lock(db, wait=True):
            calls.append(wait)
            yield
        monkeypatch.setattr(watch, 'engine_lock', fake_lock)
        main(['--db', str(tmp_path / 'm.db'), '--symbol', 'BTCUSDT', 'add-level', '--price', '100', '--note', 'n'])
        assert calls == [True]
        conn = store.connect(tmp_path / 'm.db')
        assert conn.execute("SELECT price, note FROM levels WHERE source='manual'").fetchone()[:] == (100.0, 'n')

    def test_weekly_counts_forward_and_history_apart_and_holds_back_the_last_six_candles(self, monkeypatch, tmp_path,
                                                                                        from_2020_off):
        import re
        conn = engine_db(monkeypatch)
        levels.update_levels(conn, now=1)
        levels.update_visits(conn, now=1)
        start, week = 14 * DAY, 21 * DAY
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('engine_start', ?)", (str(start),))
        text = reports.weekly_report(conn, week)
        found = [int(n) for n in re.findall(r'Decided visits: (\d+)', text)]
        count = lambda lo, hi: conn.execute("SELECT COUNT(*) FROM level_visits WHERE touch_time>=? AND touch_time<? "
                                            "AND result IS NOT NULL", (lo, hi - 6 * H4)).fetchone()[0]
        assert found == [count(start, week + 7 * DAY), count(0, start)] and min(found) > 0


@pytest.mark.unit
def test_sync_really_waits_for_a_lock_held_by_another_process(tmp_path):
    """A second process holds the engine lock for 1.5s; `sync` must not start before it lets go."""
    import subprocess
    import sys
    import time
    db = tmp_path / 'm.db'
    holder = subprocess.Popen([sys.executable, '-c', (
        "import fcntl, sys, time\n"
        f"fh = open({str(db) + '.watch.lock'!r}, 'w'); fcntl.flock(fh, fcntl.LOCK_EX)\n"
        "print('held', flush=True); time.sleep(1.5)\n")], stdout=subprocess.PIPE, text=True)
    assert holder.stdout.readline().strip() == 'held'
    started = time.time()
    with watch.engine_lock(db):                 # the same call `sync` and `add-level` make
        waited = time.time() - started
    holder.wait()
    assert waited >= 1.0
    with pytest.raises(watch.EngineBusy):       # and `watch` gives up instead of waiting
        with open(str(db) + '.watch.lock', 'w') as fh:
            import fcntl
            fcntl.flock(fh, fcntl.LOCK_EX)
            with watch.engine_lock(db, wait=False):
                pass


@pytest.mark.unit
def test_failed_sync_does_not_save_partial_work(monkeypatch, tmp_path, from_2020_off):
    conn = store.connect(':memory:')

    def half_then_fail(conn_, now_ms=None, get_json=None, get_funding_json=None, **kw):
        conn_.execute("INSERT INTO meta(key, value) VALUES ('partial', 'x')")   # unsaved work
        raise OSError('network gone')
    monkeypatch.setattr(pipeline, 'sync', half_then_fail)
    out = watch.run_cycle(conn, symbols=['BTCUSDT'], now=900 * HOUR, report_dir=tmp_path, version='v')
    assert 'sync_error' in out['BTCUSDT']
    assert conn.execute("SELECT value FROM meta WHERE key='partial'").fetchone() is None
    assert conn.execute("SELECT value FROM meta WHERE key='sync_status:BTCUSDT'").fetchone()[0].startswith('error ')


@pytest.mark.unit
class TestFrozenRule:
    RULE = {'name': 't', 'frozen_ms': 14 * DAY, 'sources': ('swing_4h', 'swing_1d'),
            'support_rsi_below': 50, 'resistance_rsi_above': 50, 'min_visits': 100}

    def test_rsi_condition_is_strict_and_depends_on_the_side(self):
        r = {'support_rsi_below': 30, 'resistance_rsi_above': 70}
        assert reports.rule_qualifies(r, 'support', 29.9) and not reports.rule_qualifies(r, 'support', 30)
        assert reports.rule_qualifies(r, 'resistance', 70.1) and not reports.rule_qualifies(r, 'resistance', 70)
        assert not reports.rule_qualifies(r, 'support', 80) and not reports.rule_qualifies(r, 'resistance', 20)
        assert not reports.rule_qualifies(r, 'support', None)

    def _report(self, monkeypatch, rule):
        conn = engine_db(monkeypatch)
        levels.update_levels(conn, now=1)
        levels.update_visits(conn, now=1)
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('engine_start', ?)", (str(7 * DAY),))
        monkeypatch.setattr(settings, 'FROZEN_RULES', (rule,))
        return conn, reports.weekly_report(conn, 21 * DAY)

    def test_counts_only_qualifying_swing_visits_after_the_freeze(self, monkeypatch, from_2020_off):
        import re
        conn, text = self._report(monkeypatch, self.RULE)
        rows = conn.execute("SELECT symbol, touch_time, source, side, rsi14, result FROM level_visits WHERE touch_time>=? AND touch_time<? "
                            "AND result IS NOT NULL", (14 * DAY, 28 * DAY - 6 * H4)).fetchall()
        want = [r for r in rows if r['source'] in self.RULE['sources']
                and reports.rule_qualifies(self.RULE, r['side'], r['rsi14'])]
        k, n = (int(x) for x in re.search(r'\| rule t \| (\d+)/(\d+)', text).groups())
        assert (k, n) == (sum(r['result'] == 'held' for r in want), len(want)) and n > 0
        assert len(want) < len(rows)                                    # the filter really removes visits
        touches = len({(r['symbol'], r['touch_time']) for r in want})
        assert f"These {n} visits come from {touches} distinct touch candles" in text
        assert text.index('## Rule t') < text.index('## Forward evidence')
        assert f'too early: {n}/100 decided visits' in text and 'same RSI condition' in text
        away = [r['away_12'] for r in conn.execute(
            "SELECT side, rsi14, result, away_12, source FROM level_visits WHERE touch_time>=? AND touch_time<? "
            "AND result='held' AND away_12 IS NOT NULL", (14 * DAY, 28 * DAY - 6 * H4))
            if r['source'] in self.RULE['sources'] and reports.rule_qualifies(self.RULE, r['side'], r['rsi14'])]
        net = reports.pct(sum(away) / len(away) - settings.ROUND_TRIP_FEE_BPS / 10_000)
        assert f"| rule t | {k}/{n}" in text and f"{net} ({len(away)} holds)" in text
        rk, rn, _ = reports._random_rule(conn, 14 * DAY, 28 * DAY - 6 * H4, self.RULE, 100, seed=3)   # same window
        assert rn > 0 and f"| random flat lines, same RSI condition | {rk}/{rn} =" in text

    def test_reaching_the_visit_count_gives_a_verdict(self, monkeypatch, from_2020_off):
        _, text = self._report(monkeypatch, {**self.RULE, 'min_visits': 1})
        assert 'too early' not in text and ('pass bar met' in text or 'pass bar not met' in text)

    def test_no_section_before_the_rule_was_frozen(self, monkeypatch, from_2020_off):
        _, text = self._report(monkeypatch, {**self.RULE, 'frozen_ms': 40 * DAY})
        assert '## Rule t' not in text

    def test_v1b_is_stored_exactly_as_frozen(self):
        assert settings.FROZEN_RULES == ({'name': 'v1b', 'frozen_ms': 1793706420000,
                                          'sources': ('swing_4h', 'swing_1d'),
                                          'support_rsi_below': 30, 'resistance_rsi_above': 70, 'min_visits': 100},)

    @pytest.mark.parametrize('k, n, rk, rn, away, expect', [
        (10, 50, 10, 50, [0.01], 'too early: 50/100'),
        (80, 100, 0, 0, [0.01], 'no random-line comparison'),
        (80, 100, 20, 100, [], 'the move after holds is unavailable'),
        (80, 100, 20, 100, [0.005], 'pass bar met:'),
        (80, 100, 20, 100, [0.0005], 'not positive after cost'),     # positive before the 0.10% cost only
        (80, 100, 20, 100, [-0.01], 'not positive after cost'),
        (20, 100, 80, 100, [0.01], 'below random lines'),
        (50, 100, 48, 100, [0.01], 'ranges overlap'),
    ])
    def test_verdict_applies_the_pass_bar(self, k, n, rk, rn, away, expect):
        assert expect in reports.rule_verdict(k, n, rk, rn, away, 0.001, 100)

    def test_random_baseline_uses_the_same_rsi_condition_on_the_touch_candle(self, monkeypatch, from_2020_off):
        conn = engine_db(monkeypatch)
        levels.update_levels(conn, now=1)
        levels.update_visits(conn, now=1)                      # random lines run per coin found in level_visits
        lo, hi = 14 * DAY, 28 * DAY
        got = reports._random_rule(conn, lo, hi, self.RULE, 20, seed=5)
        rnd, held, total, every, away = random.Random(5), 0, 0, 0, []
        for c, a, b in reports._window_candles(conn, lo, hi):
            for v in reports._flat_line_visits(c, a, b, rnd, 20):
                every += 1
                if reports.rule_qualifies(self.RULE, v['side'], c[v['touch_i']]['rsi14']):
                    total += 1
                    held += v['result'] == 'held'
                    if v['result'] == 'held' and 12 in v['away']:
                        away.append(v['away'][12])
        assert got == (held, total, away) and 0 < total < every

    def test_weekly_report_leaves_the_current_symbol_alone(self, monkeypatch, from_2020_off):
        conn = engine_db(monkeypatch)
        levels.update_levels(conn, now=1)
        levels.update_visits(conn, now=1)
        monkeypatch.setattr(settings, 'FROZEN_RULES', (self.RULE,))
        monkeypatch.setattr(settings, 'SYMBOL', 'NOT-A-COIN')
        reports.weekly_report(conn, 21 * DAY)
        assert settings.SYMBOL == 'NOT-A-COIN'


WEEK = settings.TIMEFRAMES['1w']


@pytest.mark.unit
class TestWeeklyLevels:
    """Weekly swings and weekly EMAs are levels too, judged on 4h closes, without changing the 4h/daily levels."""
    OLD = {'SWING_TIMEFRAMES': ('4h', '1d'),
           'EMA_LEVELS': (('ema50', '4h'), ('ema200', '4h'), ('ema50', '1d'), ('ema200', '1d'))}

    def test_settings_add_weekly_swings_and_emas(self):
        assert settings.SWING_TIMEFRAMES == ('4h', '1d', '1w')
        assert settings.LEVEL_LIFETIME_DAYS == {'4h': 90, '1d': 365, '1w': 1095, 'manual': 3650}
        assert settings.EMA_LEVELS == self.OLD['EMA_LEVELS'] + (('ema50', '1w'), ('ema200', '1w'))

    def test_weekly_forward_start_is_fixed_and_covers_every_late_source(self):
        assert settings.SOURCE_FORWARD_START == {'swing_1w': 1793779200000, 'ema50_1w': 1793779200000,
                                                 'ema200_1w': 1793779200000}          # 2026-11-04 08:00 UTC
        current = {f'swing_{tf}' for tf in settings.SWING_TIMEFRAMES} | {f'{n}_{tf}' for n, tf in settings.EMA_LEVELS}
        original = {f'swing_{tf}' for tf in self.OLD['SWING_TIMEFRAMES']} | {f'{n}_{tf}' for n, tf in self.OLD['EMA_LEVELS']}
        assert current - original == set(settings.SOURCE_FORWARD_START)

    def test_weekly_swing_is_known_after_three_more_weeks_and_lasts_three_years(self):
        highs = [1, 2, 3, 9, 3, 2, 1, 1, 1]
        cs = [{'open_time': i * WEEK, 'high': h, 'low': 0.5 - i * 0.01} for i, h in enumerate(highs)]
        found = [x for x in levels.find_swings(cs, '1w') if x['kind'] == 'H']
        assert [(x['source'], x['formed_at'], x['known_at'], x['expires_at'], x['price']) for x in found] == [
            ('swing_1w', 3 * WEEK, 7 * WEEK, 7 * WEEK + 1095 * DAY, 9)]

    def test_weekly_ema_comes_from_the_last_closed_week(self, monkeypatch, from_2020_off):
        monkeypatch.setattr(settings, 'SYMBOL', 'BTCUSDT')
        conn = store.connect(':memory:')
        w0 = 10 * WEEK
        for w in range(4):                                    # week 0: EMA50 100; from week 1: 200
            put(conn, '1w', w0 + w * WEEK, 95, 96, 94, 95, 100.0 if w == 0 else 200.0, None)
        for i in range(3 * 42):                               # candles 42 (week 1's first) and 50 push into 100
            put(conn, '4h', w0 + i * H4, *((95, 99.95, 95, 98) if i in (42, 50) else (95, 96, 94, 95)))
        conn.commit()
        levels.update_visits(conn, now=1)
        rows = conn.execute("SELECT touch_time, level_price, result FROM level_visits WHERE source='ema50_1w' "
                            "ORDER BY touch_time").fetchall()
        assert [tuple(r) for r in rows] == [(w0 + 42 * H4, 100.0, 'held'), (w0 + 50 * H4, 100.0, 'held')]

    def test_adding_weekly_levels_leaves_4h_and_daily_levels_and_visits_unchanged(self, monkeypatch, from_2020_off):
        def build(**over):
            for k, v in over.items():
                monkeypatch.setattr(settings, k, v)
            conn = engine_db(monkeypatch, n_hours=24 * 7 * 14)
            levels.update_levels(conn, now=1)
            levels.update_visits(conn, now=1)
            return conn
        sql = {'levels': "SELECT * FROM levels WHERE source NOT LIKE '%1w' ORDER BY level_id",
               'level_visits': "SELECT * FROM level_visits WHERE source NOT LIKE '%1w' ORDER BY level_id, touch_time"}
        new = build()
        weekly = new.execute("SELECT COUNT(*) FROM level_visits WHERE source='swing_1w'").fetchone()[0]
        got = {t: [tuple(r) for r in new.execute(q)] for t, q in sql.items()}
        old = build(**self.OLD)
        assert weekly > 0 and got['level_visits']
        assert got == {t: [tuple(r) for r in old.execute(q)] for t, q in sql.items()}
        assert not old.execute("SELECT COUNT(*) FROM levels WHERE source LIKE '%1w'").fetchone()[0]
        stored = [tuple(r) for r in new.execute("SELECT formed_at, known_at, kind, price FROM levels "
                                                "WHERE source='swing_1w' ORDER BY known_at, formed_at, kind")]
        expect = [(x['formed_at'], x['known_at'], x['kind'], x['price']) for x in levels.find_swings(
            levels.load_candles(new, '1w', since=settings.HISTORY_START_MS), '1w')]
        assert stored and stored == sorted(expect)

    def test_forward_scorecard_counts_late_sources_only_from_their_start(self, monkeypatch, from_2020_off):
        conn = engine_db(monkeypatch, n_hours=24 * 7 * 14)
        levels.update_levels(conn, now=1)
        levels.update_visits(conn, now=1)
        decided = lambda src: [r[0] for r in conn.execute(
            "SELECT touch_time FROM level_visits WHERE source=? AND result IS NOT NULL ORDER BY touch_time", (src,))]
        times = decided('swing_1w')
        assert len(times) >= 2
        straddle = conn.execute("SELECT touch_time FROM level_visits WHERE source='swing_1w' AND result='held' "
                                "AND touch_time>? ORDER BY touch_time LIMIT 1", (times[0],)).fetchone()[0]
        cut = straddle + 1                                    # this visit touches before the cut, is decided after
        hi = times[-1] + 7 * H4
        count = lambda lines, src: sum(line.startswith(f'| {src} |') for line in lines)
        monkeypatch.setattr(settings, 'SOURCE_FORWARD_START', {'swing_1w': cut})
        fwd = reports._scorecard(conn, 0, hi, 'f', seed=1, forward=True)
        hist = reports._scorecard(conn, 0, hi, 'h', seed=1)
        n_all = int(hist[2].split()[2])
        assert int(fwd[4].split()[2]) == n_all - sum(t < cut for t in times)
        assert fwd[2].startswith('Added after the forward start, so counted only from swing_1w')
        assert count(fwd, 'swing_1w') == count(hist, 'swing_1w') == 1
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('engine_start', ?)", (str(times[0]),))
        week = reports.weekly_report(conn, (times[-1] // (7 * DAY)) * 7 * DAY)
        assert 'Added after the forward start, so counted only from swing_1w' in week.split('## History')[0]
        assert 'Added after the forward start' not in week.split('## History')[1]
        monkeypatch.setattr(settings, 'SOURCE_FORWARD_START', {'swing_1w': -1})
        assert reports._scorecard(conn, 0, hi, 'f', seed=1, forward=True) == reports._scorecard(conn, 0, hi, 'f', seed=1)
