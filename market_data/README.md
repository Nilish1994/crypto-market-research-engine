# Market-data recorder

Records Binance USD-M Futures **BTCUSDT** (TradingView `BINANCE:BTCUSDT.P`) closed candles on
**5m, 15m, 1h, 4h, 1d and 1w**. For every candle it stores:

- EMA 50 and EMA 200
- RSI 14
- volume and a 20-candle volume average

It marks the owner's **Strategy-1 "EMA Retest Zones"** signals and measures what price did afterwards.

It is separate from the trading bot:

- own database `data/market_data.db`
- public market data only
- no API keys and no orders

## Commands (all times UTC)

```bash
python3 -m market_data sync                                  # download new closed candles, update everything
python3 -m market_data show --tf 1h --at "2026-10-30 12:00"  # one candle with its EMAs, RSI, volume, signals
python3 -m market_data signals --tf 4h --last 10             # latest Strategy-1 signals
python3 -m market_data snapshot --at "2026-10-30 12:07"      # all six timeframes at one moment
python3 -m market_data report                                # how signals behaved afterwards
python3 -m market_data gaps                                  # missing candles in Binance's history
python3 -m market_data test --tf 4h --signal LR50           # fair test of one signal (see below)
```

Other coins: add `--symbol` before the command, for example
`python3 -m market_data --symbol ETHUSDT sync --tf 4h 1d`, then
`python3 -m market_data --symbol ETHUSDT test --tf 4h --signal LR50`. Any Binance USD-M perpetual works.
All coins share one database, kept apart by the `symbol` column.

`sync` only adds what is missing, so it is safe to run again at any time. Run it every 5 minutes
to keep the data live. No scheduler is installed; that is the owner's choice. For example, this
crontab line:

```
*/5 * * * *  cd /path/to/crypto-market-research-engine && python3 -m market_data sync >> data/market_data_sync.log 2>&1
```

`sync --rebuild` recomputes indicators, signals, context and outcomes from the stored candles.

## Checking against TradingView

1. Open `BINANCE:BTCUSDT.P` and set the chart timezone to **UTC**.
2. Add EMA 50, EMA 200, RSI 14 and the Strategy-1 indicator.
3. Pick a candle and run `show --tf <timeframe> --at "<candle open time>"`.
4. Open, high, low, close, volume, EMAs and RSI should match the chart. Signals should land on the
   same candles.

TradingView shows the time a candle **opens**; so does this tool.

## Tables

| Table | One row per | Contents |
|---|---|---|
| `candles` | closed candle | OHLC, volume (BTC), quote volume (USDT), taker-buy volume |
| `indicators` | candle | `ema50`, `ema200`, `rsi14`, `volume_sma20`; empty until enough history |
| `signals` | signal | LR50 / LR200 / SR50 / SR200, the close and the EMA that was retested |
| `signal_context` | signal × other timeframe | the latest candle of that timeframe that had **closed** when the signal closed, with its EMAs, RSI and trend |
| `outcomes` | signal × 3 / 6 / 12 candles | best and worst move after the signal, and where price closed |
| `funding` | 8-hour funding settlement | Binance funding rate (positive = longs pay shorts) |

## How the numbers are made

- **Indicators:** follow TradingView's built-ins.
  - `ta.ema` is seeded with an SMA.
  - `ta.rsi` uses Wilder smoothing.
  - `ta.sma` for the volume average.
  - They are computed from the first Futures candle (September 2019), so they match a chart with
    full history.
- **EMA 200 warm-up:** it needs 200 candles, so the weekly EMA 200 only exists from about
  July 2023.
- **Strategy-1:** copied rule for rule from the Pine script; see `strategy1.py`.
- **Context:** never uses a candle that was still forming (no look-ahead).
- **Outcomes:** measured from the signal candle's close, in the signal's direction.

## Fair test of a signal

`test --tf <timeframe> --signal <LR50|LR200|SR50|SR200>` checks whether a signal adds anything.

It turns signals into trades the way you could actually take them:

- **Entry:** the next candle's open.
- **Exit:** after 6, 12, 24 and 42 candles (change with `--hold`).
- **Costs:** a 0.10% round-trip fee (`--fee-bps`) plus the real Binance funding paid or received.
- **No double counting:** a signal that fires while a trade is still open is skipped.

The result is compared with **random entries in the same trend**. LR signals already require an
uptrend and SR signals a downtrend, so a signal only shows value if it clearly beats random trend
entries:

- "Beats random in" of about 95% or more (p ≤ 0.05) on a holding time chosen in advance is the
  usual bar.
- Trying several holding times and picking the best one makes a lucky pass more likely.

The test also splits results by period, by year and by daily trend. Groups under about 30 trades
are noise.

## Research engine

`python3 -m market_data watch` runs one cycle for BTCUSDT, ETHUSDT and SOLUSDT
(change with `MARKET_DATA_WATCH_SYMBOLS`). The systemd timer `deploy/market-data-watch.timer` runs it
20 seconds after every 5-minute close. Each cycle:

1. downloads new closed candles and funding for every coin
2. when a new 4h candle has closed: stores newly known levels and recomputes level visits
3. writes any report that is due and missing: `data/reports/daily/YYYY-MM-DD.md` from 00:05 UTC and
   `data/reports/weekly/YYYY-Www.md` from Monday 00:05 UTC. Each is written once.

Every writer (`watch`, `sync`, `add-level`) holds the same lock file, so a cycle never overlaps a
manual sync, rebuild or level. A failure for one coin, in downloading or in levels, does not stop
the others; `meta.sync_status:<coin>` shows the latest download result.

It uses public data only. It sends no messages and places no orders.

**Levels** (`levels` table) are always stored with `known_at`, the first moment they could be known:

- **4h, daily and weekly swing highs/lows:** strictly beyond the 3 candles on each side, known when the 3rd
  candle after closes. A swing within 0.3% of an active level of the same timeframe does not create a new one.
  Levels expire after 90 days (4h), 365 days (daily) or 1095 days (weekly).
- **EMA50 and EMA200 on 4h, daily and weekly:** each 4h candle uses the value known when it opened (for daily
  and weekly, the last closed candle).
- Weekly levels were added on 2026-11-03, after the forward start. The forward scorecard counts them only
  from `SOURCE_FORWARD_START` (2026-11-04 08:00 UTC).
- **Your own levels:** `python3 -m market_data --symbol BTCUSDT add-level --price 76051 --note "Sep support"`.

**Visits** (`level_visits` table) are judged on 4h closes:

- **Approach:** 6 closes on one side, and the candle before the touch clear of the ±0.15% zone.
- **Touch:** a candle reaches the ±0.15% zone.
- **Broke:** a close beyond the zone within 6 candles. Otherwise the level **held**.
- **Fake-out:** a break where price is back on the old side 6 candles later.
- **Recorded with each visit:** test count, volume vs its 20-candle average, taker-buy share, RSI 14, 4h and
  daily trend, funding, and the move 6, 12 and 24 candles after the visit was decided.
- **Records are never rewritten:** a row is final once complete (`complete = 1`). `first_logged_at`
  shows when the engine first wrote it. Unfinished rows that a later recompute no longer produces
  are removed.
- **Code version:** `code_version` on every level and visit is the git commit of the engine that
  wrote it (`-dirty` if `market_data/` had uncommitted changes). The weekly scorecard lists the
  versions behind its numbers.

**Forward evidence** means visits after `meta.engine_start`. The weekly scorecard keeps it separate
from history (2020 onward), which was computed after the fact and is context only. The scorecard
compares each kind of level with random price lines, scored the same way. Visits from the last 6
candles of a period wait for the next report, because a hold takes 6 candles to decide and a break
does not. If `engine_start` is ever lost, the next cycle sets it to that moment, which only moves
visits from forward to history.

```bash
python3 -m market_data watch                                 # one cycle now
python3 -m market_data --symbol ETHUSDT levels               # levels nearest to price
python3 -m market_data daily-report --date 2026-10-31        # print a daily summary
python3 -m market_data weekly-report --date 2026-10-26       # print a scorecard (any day in the week)
```

Timer control:

```bash
systemctl list-timers market-data-watch.timer              # next run
journalctl -u market-data-watch.service -n 50              # recent cycles
systemctl disable --now market-data-watch.timer            # stop the engine
```

The timer runs a separate, pinned copy of the code: the git worktree `/srv/projects/market-data-engine`,
checked out at a fixed commit. Branch switches or edits in the main checkout don't change what runs.
It still uses the database and reports in `/srv/projects/trading-bot/data/` (`MARKET_DATA_DB`).
To move the engine to a newer commit, after review:

```bash
git -C /srv/projects/market-data-engine checkout --detach <commit>
```

## What the report is not

Outcomes describe price movement after a signal. They are **not trade results**:

- the Pine script defines no stop, target or exit
- no fees or funding are applied
- each signal is counted on its own, even when signals overlap

The report also shows a "no strategy" baseline for every timeframe. A signal is only interesting
if it clearly beats that baseline.
