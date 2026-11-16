# Crypto market research engine

[![tests](https://github.com/Nilish1994/crypto-market-research-engine/actions/workflows/tests.yml/badge.svg)](https://github.com/Nilish1994/crypto-market-research-engine/actions/workflows/tests.yml)
[![DOI](https://zenodo.org/badge/1457477888.svg)](https://doi.org/10.5281/zenodo.24214968)

A Python research engine that records Binance USD-M Futures market data and tests whether simple
trading signals and support/resistance levels actually beat chance. It never places orders: it uses
public market data only and needs no API keys.

It has run unattended on a Linux VPS under a systemd timer, collecting BTCUSDT, ETHUSDT and SOLUSDT
every 5 minutes.

## What it does

- **Records market data:** closed candles on 5m, 15m, 1h, 4h, 1d and 1w, plus 8-hour funding rates,
  stored in SQLite. Syncs are incremental and safe to re-run.
- **Computes indicators that match TradingView:** EMA 50/200 (SMA-seeded), RSI 14 (Wilder smoothing)
  and volume averages, computed from the first futures candle so values match a full-history chart.
- **Finds support/resistance levels:** swing highs/lows and EMA levels on 4h, daily and weekly, each
  stored with the moment it first became knowable, so there is no look-ahead.
- **Logs every level visit and its outcome:** approach, touch, hold, break or fake-out, with volume,
  taker-buy share, RSI, trend and funding at the time.
- **Tests signals fairly:** turns signals into realistic trades (next-candle entry, fees, real funding)
  and compares them with random entries in the same trend, with a significance estimate.
- **Writes reports:** daily summaries and a weekly scorecard that keeps forward (live) evidence
  separate from history computed after the fact.
- **Runs pre-registered forward tests of frozen rules.** A rule is written down in full before any data that
  counts exists, then scored only on candles that arrive afterwards, with one test fixed in advance:
  - **v1b**: a level rule
  - **s1h12**: the Strategy-1 EMA retest on 1h candles, BTC+ETH+SOL pooled, 12-hour exit, counting from
    2026-11-23 00:00 UTC. See [`docs/frozen-rule-s1h12.md`](docs/frozen-rule-s1h12.md).


## Design choices

- **No look-ahead.** Context only uses candles that had already closed; levels carry a `known_at` time.
- **Append-only evidence.** A completed visit is never rewritten, and every row records the git
  commit of the engine version that wrote it.
- **Safe concurrency.** Every writer holds the same file lock, so a scheduled cycle never overlaps a
  manual sync or rebuild.
- **Failure isolation.** A download or level failure for one coin doesn't stop the others.
- **Standard library only.** The engine itself has no third-party dependencies; pytest is used for tests.
- **Hardened deployment.** The systemd unit runs with `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome` and a single writable data path.

## Quick start

Tested on Python 3.12.

```bash
python3 -m market_data sync                       # download candles and compute everything
python3 -m market_data signals --tf 4h --last 10  # latest signals
python3 -m market_data test --tf 4h --signal LR50 # fair test of one signal vs random entries
python3 -m market_data watch                      # one research cycle (levels, visits, reports)
```

Data is written to `data/market_data.db` (set `MARKET_DATA_DB` to change it).

Full command reference, table layout and methodology: [`market_data/README.md`](market_data/README.md).

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

151 tests cover indicators, signal rules, level detection, visit classification, funding paging,
the fair test, report generation, locking and the s1h12 forward test (signals, time exit, funding, the
start-time cut, void rules, the 300/600 gates and the clustered test). They run in CI on every push.

## Layout

| Path | Contents |
|---|---|
| `market_data/` | The engine: Binance client, indicators, levels, pipeline, store, reports, CLI |
| `tests/` | pytest suite |
| `deploy/` | systemd service and timer for running a cycle every 5 minutes |
| `docs/` | Pre-registration of the frozen rule s1h12 |

## Changes

- **1.1.0 (2026-11-16):** the pre-registered forward test of frozen rule s1h12 (`market_data/forward_rule.py`,
  `market_data/forward_report.py`, its section in the weekly scorecard, 70 new tests,
  `docs/frozen-rule-s1h12.md`).
- **1.0.0 (2026-11-14):** first public release.

## Disclaimer

This is a research tool, not financial advice and not a trading system.

## License

MIT
