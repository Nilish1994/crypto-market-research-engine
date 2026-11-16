# Frozen rule s1h12: a pre-registered forward test

This document fixes the rule, the test and the stop rule **before** any data that counts exists. It was written on
2026-11-16; counting starts on **2026-11-23 00:00 UTC**. Code comments in `market_data/forward_rule.py` and
`market_data/forward_report.py` point to this text and to the research report behind it. Both are kept in the
author's working repository; this file is the public copy of the parts that matter.

Every detail below is taken from the code that runs the test (`market_data/forward_rule.py` and the `s1h12`
entry in `market_data/settings.py`). Where this text and the code could ever disagree, the pinned code is what
was frozen.

## Why one rule, tested forward

An earlier search tried about 1,200 rule variants on 2019–2025 Binance futures data: swing-level and EMA
bounces and breaks, and the Strategy-1 retest. None passed an out-of-sample check. Because so many variants
were tried, a rule needed to beat random entries by roughly 70–90 bps per trade to pass. An edge of 10–20 bps
would have been invisible.

This test asks a narrower question: **does one rule, chosen in advance, have a positive average return after
the stated costs on data that did not exist when it was chosen?** It is one test on new data, so the earlier
search's multiple-testing penalty does not apply to it.

The rule chosen is the Strategy-1 EMA retest. It already existed before the search (as a TradingView script),
so none of its settings were tuned on the search's results. It gives the most trades and the smallest spread of
the candidates, and it only trades with the trend, which has the best outside evidence. The most likely outcome,
stated in advance, is "no edge shown".

## The frozen code

The test refuses to give a result (state **void**) if any of these change after the start:

- **The pinned files:** `market_data/strategy1.py` (the signal), `market_data/indicators.py` (EMAs and RSI) and
  `market_data/forward_rule.py` (trades, counting and the test). Their combined SHA-256 is frozen in `settings.py`
  as `f81dfff61586c8622c6a3323bec47f296a95a8ef222019bbab7f068c8d81b45e`.
- **The settings:** the retest zone (0.15%) and the EMA lengths (50 and 200).

The EMAs use the TradingView method: each EMA is seeded with a simple average and computed from the first futures
candle, so values match a full-history chart (see `market_data/README.md`). The first public copy of this code is
commit `c9fe400` of this repository (2026-11-16). A tagged release with a DOI will fix a dated snapshot.

## The rule

| Item | Frozen value |
|---|---|
| Coins | BTCUSDT, ETHUSDT, SOLUSDT (Binance USD-M perpetuals), pooled into one sample |
| Timeframe | 1h closed candles |
| Signal | `market_data.strategy1.signals_for` on the closed 1h candle, with the 1h EMA50 and EMA200 and the ±0.15% zone. LR50 or LR200 = **long**; SR50 or SR200 = **short**. A candle with several signals on the same side is one trade. |
| Entry | open of the next 1h candle |
| Exit | close of the 12th 1h candle after the signal candle (time exit only; no stop, no target) |
| Costs | 5 bps fee + 2 bps slippage per side (14 bps round trip), plus the real funding settled after the entry open and before the exit close. Longs pay positive funding and shorts receive it. |
| Positions | One per coin at a time. A signal on a coin with an open trade is skipped. |
| Result per trade | direction × (exit close ÷ entry open − 1) − 14 bps − funding paid, in bps of notional |

Any change to the rule, the costs or the pinned code is a new rule with a new name, and its count starts again.

## Which trades count, and in what order

- **Start:** only signal candles that open at or after 2026-11-23 00:00 UTC count.
- **Order:** trades are sorted by signal-candle open time (UTC), then by coin in the order BTCUSDT, ETHUSDT,
  SOLUSDT. "The first 300" and "the first 600" are the first trades in this order. They don't depend on when a
  report is written.
- **A trade is complete** once its exit candle has closed and a funding settlement at or after its exit is
  stored. That shows its funding window is complete. **Missing funding is never treated as zero:** the trade
  waits.
- **Strict order:** if an earlier trade is still waiting for its data, no later trade is counted until it
  completes, so a later trade can never jump into the first 300 or 600.
- **Missing candles inside a trade's window:** if a candle needed for a trade (its entry candle through its exit
  candle) is missing, the trade cannot be measured. It is left out, and the number left out is reported.
  Candles are downloaded in order, so a missing candle is never filled in later. Leaving trades out could bias
  the sample. The 2% limit below bounds how many can be lost.

## The test

- **Sample size, fixed in advance:** the first 600 trades, which is about 15 months at about 40 trades a month.
- **Hard cap:** **2028-11-23 00:00 UTC**, 24 calendar months after the start. A trade belongs to the cap window if
  its exit candle closes by then. If 600 trades have not been reached at the cap, the test uses every complete
  trade in the window. The 300-trade check only happens if 300 trades are reached.
- **One test, computed once.** H0: the mean net return per trade is ≤ 0, tested one-sided at α = 0.05.
  - Each trade is assigned to the ISO week (UTC) of its **entry** time.
  - With N trades, mean r̄ and G weeks that contain at least one trade:

    SE² = G/(G−1) × Σ over weeks of (Σ of that week's (r − r̄))² ÷ N²

  - t = r̄ ÷ SE, and p comes from a t distribution with G − 1 degrees of freedom.
  - If fewer than two weeks contain trades, there is no test, and the result is "dead: too few trades to test".
  - Clustering by week allows trades in the same week to move together, including across coins. It still treats
    different weeks as independent, and the t distribution is an approximation.

## Power: what this test can and cannot see

Assume a spread of about 300 bps per trade and treat every trade as independent. The chance that 600 trades
show a real edge is then:

| True edge per trade | Chance the test shows it |
|---:|---:|
| 10 bps | about 20% |
| 20 bps | about 50% |
| 30 bps | about 79% |

**These figures are an independence approximation; the power of the clustered test has not been established.**
Trades in the same week are probably correlated, and that lowers power. For example, with about 9 trades a week
and a within-week correlation of 0.05, power for a 30 bps edge falls to about two-thirds. The 300-trade stop
lowers power a little more. So this test can miss an edge of 10–20 bps, the size the introduction says the
search could not see.

## The stop rule and what each result means

- **Dead early (futility):** at 300 trades, if their mean net return is ≤ 0, the test stops and records "dead".
  This check can only stop a test for failure, so it cannot create a false pass.
- **Alive:** at the end, p ≤ 0.05. This means **"a positive net mean was shown"** for this rule over this period,
  after the stated costs. It does **not** show that the retest beats random entries, or that it adds anything
  beyond following the trend. It means "worth a deeper look", not "a trade signal".
- **Dead:** at the end, p > 0.05, meaning **no edge was shown**. That is not proof that there is no edge: small
  edges are hard to see (see Power). The rule is not re-run with changed settings under the same name.
- **Void:** the pinned code or settings changed, or more than 2% of the expected 1h candles are missing. The
  missing share is measured across all three coins together, from the start to the exit of the last trade the
  result rests on (the 300th, the 600th, or the cap). A void test is restarted under a new name.

The weekly scorecard shows progress every week as "no verdict until 300 / 600 trades". That line doesn't mention
it, but the hard cap and the completeness rule above also apply. The scorecard computes no p-value in between, so
there are no repeated looks.

## Descriptions with no say in the verdict

These were declared in advance so they cannot be chosen after the fact:

- the excess over random entries in the 30 days after each signal (same coin, direction, exit and costs)
- a trend-only control: random 1h candles in the same EMA50/EMA200 trend state, to see whether the retest candle
  adds anything beyond the trend
- results by coin and by direction, and by the touch candle's volume ratio and taker-buy share
- a maker-fee sensitivity line (fees only; not a fill model)

They live in `market_data/forward_report.py`, which the verdict code does not import.

## Change and restart log

A new name doesn't erase an earlier attempt. Every change to this document after the start, and every void or
restart, is listed here with its date and reason.

| Date | What | Why |
|---|---|---|
| 2026-11-16 | First version | Pre-registration |
| 2026-11-16 | Clarified power, what "alive" means, the exact calculation, ordering, the cap date and missing data, all from the code. No change to the rule, the code or the test. | An independent review before the start |
