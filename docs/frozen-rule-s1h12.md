# Frozen rule s1h12: a pre-registered forward test

This document fixes the rule, the test and the stop rule **before** any data that counts exists. It was
written on 2026-11-16; counting starts on **2026-11-23 00:00 UTC**. Code comments in
`market_data/forward_rule.py` and `market_data/forward_report.py` point to this text and to the research
report behind it. Both are kept in the author's working repository; this file is the public copy of the
parts that matter.

## Why one rule, tested forward

An earlier search tried about 1,200 rule variants (swing-level and EMA bounces and breaks, and the
Strategy-1 retest) on 2019–2025 Binance futures data. None passed an out-of-sample check. Because so many
variants were tried, a rule needed to beat random entries by roughly 70–90 bps per trade to pass. An edge
of 10–20 bps would have been invisible. Testing **one** rule, chosen in advance and counted only on new
data, removes that multiple-testing penalty. Nobody can have tuned it to data that did not exist yet.

The rule chosen is the Strategy-1 EMA retest. It already existed before the search (as a TradingView script),
so none of its settings were tuned on the search's results. It gives the most trades and the smallest spread
of the candidates, and it only trades with the trend, which has the best outside evidence. The most likely
outcome, stated in advance, is "no edge shown".

## The rule

| Item | Frozen value |
|---|---|
| Coins | BTCUSDT, ETHUSDT, SOLUSDT (Binance USD-M perpetuals), pooled into one sample |
| Timeframe | 1h closed candles |
| Signal | `market_data.strategy1.signals_for` on the closed 1h candle, with the engine's 1h EMA50 and EMA200 and the ±0.15% zone. LR50 or LR200 = **long**; SR50 or SR200 = **short**. |
| Entry | open of the next 1h candle |
| Exit | close of the 12th 1h candle after the signal candle (time exit only; no stop, no target) |
| Costs | 5 bps fee + 2 bps slippage per side (14 bps round trip) plus the real funding settled while the trade is open |
| Positions | one per coin at a time; a signal on a coin with an open trade is skipped |
| Result per trade | direction × (exit close ÷ entry open − 1) − 14 bps − funding paid, in bps of notional |

Only signal candles that open at or after the start time count. A trade counts once its 12th candle has
closed and the funding for its window is recorded. Any change to the rule, the costs or the signal code is a
new rule with a new name, and its count starts again.

## The test

- **Sample size, fixed in advance:** 600 trades pooled, which is about 15 months at about 40 trades a month.
  The hard cap is 24 months after the start; if 600 trades aren't reached by then, the test uses what there is.
- **One test, computed once.** H0: the mean net return per trade is ≤ 0. The test is one-sided at α = 0.05. The
  standard error is clustered by ISO week, because the three coins move together, and is compared with a t
  distribution with (weeks − 1) degrees of freedom.
- **Power:** with a spread of about 300 bps per trade, 600 trades give about 80% power for a 30 bps edge.

## The stop rule

- **Dead early (futility):** at 300 trades, if the mean net return is ≤ 0, stop and record "dead". Stopping
  early for failure cannot create a false pass.
- **Alive:** at the end, p ≤ 0.05. This means the rule is worth a deeper look, not that it is a trade signal.
- **Dead:** at the end, p > 0.05, meaning no edge was shown. The rule is not re-run with changed settings
  under the same name.
- **Void:** more than 2% of 1h candles are missing in the window, or the signal code changes. A void test is
  restarted under a new name.

The weekly scorecard shows progress every week as "no verdict until 300 / 600 trades". It computes no
p-value in between, so there are no repeated looks.

## Descriptions with no say in the verdict

These were declared in advance so they can't be chosen after the fact:

- the excess over random entries in the 30 days after each signal (same coin, direction, exit and costs)
- a trend-only control: random 1h candles in the same EMA50/EMA200 trend state, to see whether the retest
  candle adds anything beyond the trend
- results by coin and by direction, and by the touch candle's volume ratio and taker-buy share
- a maker-fee sensitivity line (fees only; not a fill model)

The code imports nothing from these into the verdict.
