"""Download closed Binance USD-M Futures klines (public endpoint, no API key)."""
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from market_data import settings

log = logging.getLogger(__name__)


def parse_kline(k: list) -> dict:
    """Binance kline array -> candle dict. Volume is base asset (BTC), as TradingView shows it."""
    return {
        'open_time': int(k[0]),
        'open': float(k[1]),
        'high': float(k[2]),
        'low': float(k[3]),
        'close': float(k[4]),
        'volume': float(k[5]),
        'close_time': int(k[6]),
        'quote_volume': float(k[7]),
        'trades': int(k[8]),
        'taker_buy_volume': float(k[9]),
        'taker_buy_quote_volume': float(k[10]),
    }


def _get_json(params: dict, retries: int = 5, base_url: str = settings.KLINES_URL):
    url = f"{base_url}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code in (418, 429) or e.code >= 500:
                wait = int(e.headers.get('Retry-After') or 2 ** (attempt + 1))
                log.warning('Binance HTTP %s, retrying in %ss', e.code, wait)
                time.sleep(wait)
                continue
            raise
        except urllib.error.URLError as e:
            wait = 2 ** (attempt + 1)
            log.warning('Binance request failed (%s), retrying in %ss', e.reason, wait)
            time.sleep(wait)
    raise RuntimeError(f'Binance request failed after {retries} attempts: {params}')


def fetch_closed_klines(timeframe: str, start_ms: int, now_ms: int = None, get_json=_get_json):
    """
    Yield pages (lists of candle dicts) from start_ms onwards.

    Only candles whose close_time is before now are returned: a candle that is
    still forming is never yielded (Pine: barstate.isconfirmed).
    """
    now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    cursor = start_ms
    while True:
        rows = get_json({'symbol': settings.SYMBOL, 'interval': timeframe,
                         'startTime': cursor, 'limit': settings.KLINES_LIMIT})
        candles = [parse_kline(k) for k in rows]
        closed = [c for c in candles if c['close_time'] < now_ms]
        if closed:
            yield closed
        if len(closed) < len(candles) or len(rows) < settings.KLINES_LIMIT:
            return
        cursor = candles[-1]['open_time'] + settings.TIMEFRAMES[timeframe]
        time.sleep(settings.REQUEST_PAUSE_SEC)


def _get_funding_json(params: dict):
    return _get_json(params, base_url=settings.FUNDING_URL)


def fetch_funding(start_ms: int, get_json=_get_funding_json):
    """Yield pages of (funding_time_ms, rate) settled at or after start_ms. A positive rate means longs pay shorts."""
    cursor = start_ms
    while True:
        rows = get_json({'symbol': settings.SYMBOL, 'startTime': cursor, 'limit': settings.FUNDING_LIMIT})
        page = [(int(r['fundingTime']), float(r['fundingRate'])) for r in rows]
        if page:
            yield page
        if len(rows) < settings.FUNDING_LIMIT:
            return
        cursor = page[-1][0] + 1
        time.sleep(settings.REQUEST_PAUSE_SEC)
