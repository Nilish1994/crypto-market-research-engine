"""
One cycle of the research engine, run every 5 minutes by deploy/market-data-watch.timer:

  1. for each watched coin: download new closed candles and funding, update indicators and signals
  2. when a new 4h candle has closed: store newly known levels and recompute level visits
  3. write yesterday's daily summary and last week's scorecard if they are not written yet

Public market data only. No orders, no API keys.
Every command that writes to the database holds the same lock file, so a cycle never overlaps a
manual sync, rebuild or add-level. Rows are stamped with the git commit of the code that wrote them.
"""
import fcntl
import logging
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from market_data import levels, pipeline, reports, settings, store

log = logging.getLogger(__name__)
PACKAGE_DIR = Path(__file__).resolve().parent


class EngineBusy(Exception):
    pass


def code_version() -> str:
    """Git commit of this package's checkout, with '-dirty' if market_data/ has uncommitted changes."""
    try:
        head = subprocess.run(['git', '-C', str(PACKAGE_DIR), 'rev-parse', '--short=12', 'HEAD'],
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        dirty = subprocess.run(['git', '-C', str(PACKAGE_DIR), 'status', '--porcelain', '--', '.'],
                               capture_output=True, text=True, timeout=10, check=True).stdout.strip()
        return head + ('-dirty' if dirty else '')
    except (OSError, subprocess.SubprocessError):
        return 'unknown'


@contextmanager
def engine_lock(db_path, wait=True):
    """Exclusive lock shared by every writer. wait=False raises EngineBusy instead of waiting."""
    lock_path = Path(str(db_path) + '.watch.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, 'w') as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            raise EngineBusy(str(lock_path)) from None
        yield


def _meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


def _set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, str(value)))
    conn.commit()


def _stamp(now):
    return datetime.fromtimestamp(now / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')


def run_cycle(conn, symbols=None, now=None, get_json=None, get_funding_json=None, report_dir=None,
              version=None) -> dict:
    now = now or levels.now_ms()
    version = version or code_version()
    symbols = list(symbols or settings.WATCH_SYMBOLS)
    if _meta(conn, 'engine_start') is None:
        # Setting it later than before only moves visits from "forward" to "history", never the other way.
        if conn.execute("SELECT 1 FROM level_visits LIMIT 1").fetchone():
            log.warning('engine_start was missing; forward evidence restarts now')
        _set_meta(conn, 'engine_start', now)
    _set_meta(conn, 'code_version', version)
    summary = {'code_version': version}
    keep = settings.SYMBOL
    try:
        for sym in symbols:
            settings.SYMBOL = sym
            s = {}
            try:
                r = pipeline.sync(conn, now_ms=now, get_json=get_json, get_funding_json=get_funding_json)
                s['candles_added'] = sum(r['candles_added'].values())
                _set_meta(conn, f'sync_status:{sym}', f'ok {_stamp(now)}')
            except Exception as e:                         # one coin failing must not stop the others
                conn.rollback()                            # drop any unsaved partial work before noting the error
                log.exception('sync failed for %s', sym)
                s['sync_error'] = repr(e)
                try:
                    _set_meta(conn, f'sync_status:{sym}', f'error {_stamp(now)} {e!r}'[:300])
                except Exception:
                    log.exception('could not record sync status for %s', sym)
            try:
                last_4h = store.last_open_time(conn, 'candles', sym, settings.VISIT_TF)
                key = f'visits_through:{sym}'
                if last_4h is not None and _meta(conn, key) != str(last_4h):
                    s['levels_added'] = levels.update_levels(conn, now, version)
                    s['visits'] = levels.update_visits(conn, now, version)
                    _set_meta(conn, key, last_4h)
            except Exception as e:
                conn.rollback()
                log.exception('level update failed for %s', sym)
                s['levels_error'] = repr(e)
            summary[sym] = s
    finally:
        settings.SYMBOL = keep
    try:
        summary['reports_written'] = reports.write_due_reports(conn, now, symbols, report_dir, version)
    except Exception as e:
        log.exception('report writing failed')
        summary['reports_error'] = repr(e)
    return summary


def run_locked(db_path, **kwargs) -> dict:
    try:
        with engine_lock(db_path, wait=False):
            conn = store.connect(db_path)
            try:
                return run_cycle(conn, **kwargs)
            finally:
                conn.close()
    except EngineBusy:
        return {'skipped': 'another cycle or a manual write is running'}


def force_visit_update(conn, symbol):
    """Make the next cycle recompute visits for a coin (used after adding a level)."""
    conn.execute("DELETE FROM meta WHERE key=?", (f'visits_through:{symbol}',))
    conn.commit()
