"""
Command line for the market-data recorder. All times are UTC.

  python -m market_data sync                 download new closed candles and update everything
  python -m market_data sync --rebuild       recompute indicators/signals/context/outcomes from stored candles
  python -m market_data show --tf 1h --at "2026-10-30 12:00"
  python -m market_data signals --tf 4h --last 10
  python -m market_data snapshot --at "2026-10-30 12:07"
  python -m market_data report [--horizon 12]
  python -m market_data test --tf 4h --signal LR50 [--hold 6 12 24 42] [--fee-bps 10]
  python -m market_data gaps
  python -m market_data --symbol ETHUSDT sync --tf 4h 1d     (any Binance USD-M perpetual)

Research engine:
  python -m market_data watch                one cycle for every watched coin (the systemd timer runs this)
  python -m market_data levels [--count 6]   levels nearest to the current price
  python -m market_data add-level --price 76051 --note "Sep support"
  python -m market_data daily-report --date 2026-10-31
  python -m market_data weekly-report --date 2026-10-26     (any day in that week)
"""
import argparse
import json
import logging
from datetime import datetime, timedelta, timezone

from market_data import fair_test, levels, pipeline, report, reports, settings, store, watch
from market_data.indicators import trend


def to_ms(text: str) -> int:
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def _num(x, digits=2):
    return '—' if x is None else f"{x:,.{digits}f}"


def cmd_show(conn, args):
    at = to_ms(args.at)
    sql = ("SELECT c.*, i.ema50, i.ema200, i.rsi14, i.volume_sma20 FROM candles c LEFT JOIN indicators i "
           "ON i.symbol=c.symbol AND i.timeframe=c.timeframe AND i.open_time=c.open_time "
           "WHERE c.symbol=? AND c.timeframe=? AND c.open_time<=? ORDER BY c.open_time DESC LIMIT 1")
    row = conn.execute(sql, (settings.SYMBOL, args.tf, at)).fetchone()   # the candle containing `at`
    if row is None:
        print('No candle stored for that time.')
        return
    sigs = [r[0] for r in conn.execute(
        "SELECT signal FROM signals WHERE symbol=? AND timeframe=? AND open_time=?",
        (settings.SYMBOL, args.tf, row['open_time']))]
    print(f"{settings.SYMBOL} {args.tf} candle opening {fmt_ms(row['open_time'])} UTC")
    print(f"  open {_num(row['open'])}  high {_num(row['high'])}  low {_num(row['low'])}  close {_num(row['close'])}")
    print(f"  volume {_num(row['volume'], 3)} BTC  (volume SMA20 {_num(row['volume_sma20'], 3)})")
    print(f"  EMA50 {_num(row['ema50'])}  EMA200 {_num(row['ema200'])}  RSI14 {_num(row['rsi14'])}")
    print(f"  Strategy-1 signals: {', '.join(sigs) if sigs else 'none'}")


def cmd_signals(conn, args):
    rows = conn.execute(
        "SELECT s.*, c.open, c.high, c.low FROM signals s JOIN candles c ON c.symbol=s.symbol "
        "AND c.timeframe=s.timeframe AND c.open_time=s.open_time "
        "WHERE s.symbol=? AND s.timeframe=? ORDER BY s.open_time DESC LIMIT ?",
        (settings.SYMBOL, args.tf, args.last)).fetchall()
    print(f"Last {len(rows)} Strategy-1 signals on {args.tf} (candle open time, UTC):")
    for r in reversed(rows):
        print(f"  {fmt_ms(r['open_time'])}  {r['signal']:<5}  close {_num(r['close'])}  "
              f"low {_num(r['low'])}  high {_num(r['high'])}  EMA {_num(r['ema'])}")


def cmd_snapshot(conn, args):
    at = to_ms(args.at)
    print(f"Latest CLOSED candle of each timeframe as of {fmt_ms(at)} UTC:")
    for tf in settings.TIMEFRAMES:
        row = pipeline.latest_closed(conn, tf, at - 1)
        if row is None:
            print(f"  {tf:>3}: no data")
            continue
        print(f"  {tf:>3}: opened {fmt_ms(row['open_time'])}  close {_num(row['close'])}  "
              f"EMA50 {_num(row['ema50'])}  EMA200 {_num(row['ema200'])}  RSI14 {_num(row['rsi14'])}  "
              f"trend {trend(row['ema50'], row['ema200']) or '—'}")


def cmd_gaps(conn, args):
    for tf in settings.TIMEFRAMES:
        n = conn.execute("SELECT COUNT(*), MIN(open_time), MAX(open_time) FROM candles WHERE symbol=? AND timeframe=?",
                         (settings.SYMBOL, tf)).fetchone()
        if not n[0]:
            print(f"{tf:>3}: no candles")
            continue
        missing, examples = pipeline.find_gaps(conn, tf)
        print(f"{tf:>3}: {n[0]:,} candles {fmt_ms(n[1])} → {fmt_ms(n[2])}, {missing} missing")
        for prev, nxt in examples:
            print(f"       gap after {fmt_ms(prev)} until {fmt_ms(nxt)}")


def cmd_levels(conn, args):
    last = conn.execute("SELECT open_time, close FROM candles WHERE symbol=? AND timeframe='5m' ORDER BY open_time DESC LIMIT 1",
                        (settings.SYMBOL,)).fetchone()
    if last is None:
        print('No candles stored yet.')
        return
    at = last['open_time'] + settings.TIMEFRAMES['5m']
    groups = levels.group_nearby(levels.active_levels(conn, at))
    above = [g for g in groups if g[0]['price'] > last['close']][:args.count]
    below = [g for g in groups if g[0]['price'] <= last['close']][-args.count:]
    print(f"{settings.SYMBOL} last close {_num(last['close'])} at {fmt_ms(at)} UTC")
    for g in list(reversed(above)) + [None] + list(reversed(below)):
        if g is None:
            print(f"  {'— price —':>12}")
            continue
        parts = []
        for x in g:
            n = conn.execute("SELECT COUNT(*), SUM(result='held'), SUM(result='broke') FROM level_visits "
                             "WHERE symbol=? AND level_id=?", (settings.SYMBOL, x['level_id'])).fetchone()
            note = f" '{x['note']}'" if x.get('note') else ''
            parts.append(f"{x['source']}{note} since {fmt_ms(x['known_at'])[:10]}: {n[0]} visits "
                         f"({n[1] or 0} held, {n[2] or 0} broke)")
        print(f"  {_num(g[0]['price']):>12}  {g[0]['price'] / last['close'] * 100 - 100:+6.2f}%  " + ' | '.join(parts))


def _day_start(text):
    return to_ms(datetime.fromisoformat(text).strftime('%Y-%m-%d'))


def main(argv=None):
    parser = argparse.ArgumentParser(prog='python -m market_data', description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=str(settings.DB_PATH))
    parser.add_argument('--symbol', default=settings.SYMBOL, type=str.upper,
                        help='Binance USD-M perpetual, e.g. BTCUSDT, ETHUSDT, SOLUSDT (default %(default)s)')
    sub = parser.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('sync')
    p.add_argument('--tf', nargs='*', choices=list(settings.TIMEFRAMES))
    p.add_argument('--rebuild', action='store_true')
    p = sub.add_parser('show')
    p.add_argument('--tf', required=True, choices=list(settings.TIMEFRAMES))
    p.add_argument('--at', required=True)
    p = sub.add_parser('signals')
    p.add_argument('--tf', required=True, choices=list(settings.TIMEFRAMES))
    p.add_argument('--last', type=int, default=10)
    p = sub.add_parser('snapshot')
    p.add_argument('--at', required=True)
    p = sub.add_parser('report')
    p.add_argument('--horizon', type=int, default=12, choices=settings.OUTCOME_HORIZONS)
    sub.add_parser('gaps')
    p = sub.add_parser('test', help='fair test of one signal: next-open entry, fees, funding, random baseline')
    p.add_argument('--tf', required=True, choices=list(settings.TIMEFRAMES))
    p.add_argument('--signal', required=True, choices=['LR50', 'LR200', 'SR50', 'SR200'])
    p.add_argument('--hold', type=int, nargs='+', default=[6, 12, 24, 42])
    p.add_argument('--runs', type=int, default=2000)
    p.add_argument('--fee-bps', type=float, default=settings.ROUND_TRIP_FEE_BPS)
    p.add_argument('--seed', type=int, default=7)
    sub.add_parser('watch', help='one research-engine cycle for every watched coin')
    p = sub.add_parser('levels', help='levels nearest to the current price')
    p.add_argument('--count', type=int, default=6)
    p = sub.add_parser('add-level', help='add your own level (known from now on)')
    p.add_argument('--price', type=float, required=True)
    p.add_argument('--note')
    p = sub.add_parser('daily-report')
    p.add_argument('--date', required=True, help='UTC day, e.g. 2026-10-31')
    p = sub.add_parser('weekly-report')
    p.add_argument('--date', required=True, help='any UTC day in the week')
    args = parser.parse_args(argv)

    if args.cmd == 'watch':
        logging.basicConfig(level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')
        print(json.dumps(watch.run_locked(args.db), indent=2))
        return

    settings.SYMBOL = args.symbol           # every module reads settings.SYMBOL at call time
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.cmd in ('sync', 'add-level'):   # writers wait for a running cycle to finish
        with watch.engine_lock(args.db):
            conn = store.connect(args.db)
            if args.cmd == 'sync':
                print(json.dumps(pipeline.sync(conn, timeframes=args.tf, rebuild=args.rebuild), indent=2))
            else:
                level_id = levels.add_manual_level(conn, args.price, args.note, code_version=watch.code_version())
                watch.force_visit_update(conn, settings.SYMBOL)
                print(f"Added {settings.SYMBOL} level {_num(args.price)} as {level_id}; visits are tracked from now on.")
        return
    conn = store.connect(args.db)
    if args.cmd == 'show':
        cmd_show(conn, args)
    elif args.cmd == 'signals':
        cmd_signals(conn, args)
    elif args.cmd == 'snapshot':
        cmd_snapshot(conn, args)
    elif args.cmd == 'report':
        print(report.build_report(conn, args.horizon))
    elif args.cmd == 'gaps':
        cmd_gaps(conn, args)
    elif args.cmd == 'test':
        print(fair_test.run(conn, args.tf, args.signal, holds=tuple(args.hold), runs=args.runs,
                            fee_bps=args.fee_bps, seed=args.seed))
    elif args.cmd == 'levels':
        cmd_levels(conn, args)
    elif args.cmd == 'daily-report':
        print(reports.daily_report(conn, _day_start(args.date)))
    elif args.cmd == 'weekly-report':
        day = datetime.fromisoformat(args.date)
        print(reports.weekly_report(conn, _day_start((day - timedelta(days=day.weekday())).isoformat())))


if __name__ == '__main__':
    main()
