# Sector Momentum Rotation — Design

**Status:** awaiting review
**Date:** 2026-08-16
**Supersedes:** nothing. The PEAD strategy (`HYPOTHESIS.md`) was measured and
returned NO EDGE on 2026-08-15; this is a separate hypothesis using some of
the same infrastructure.

---

## 1. The hypothesis

> **Inefficiency.** Broad equity sectors that have outperformed over the past
> 6–12 months continue to outperform over the following 1–3 months; sectors
> whose own 12-month return has turned negative continue to underperform.
>
> **Who is on the other side.** Investors rebalancing mechanically to fixed
> weights (selling winners because they grew), holders anchored to their
> purchase price taking profits early, and dip-buyers adding to falling
> sectors on valuation grounds.
>
> **Why it has not been arbitraged away.** Because capturing it requires
> years of looking wrong. Momentum rotation trailed a simple S&P tracker for
> most of 2010–2021, and gets whipsawed in range-bound markets — many small
> losses, with the payoff concentrated in a handful of large trends.

### The honest weakness of clause 3

This is a **weaker** persistence argument than PEAD's, and that must be stated
before any result arrives.

Momentum is the most documented anomaly in finance. There is a managed-futures
industry measured in hundreds of billions harvesting it, and retail momentum
ETFs exist. Its measured strength is roughly half its pre-publication level.
Anyone quoting 1990s momentum returns is quoting a number that no longer
exists.

What survives is narrower and specific to this account:

- **No career risk.** A fund manager who trails the index for three years
  loses assets and job. That constraint does not bind here.
- **No reporting cycle.** No quarterly letters, no client redemptions forcing
  liquidation at the worst moment.
- **No tax on turnover.** Inside an ISA, monthly rebalancing is free. In a
  taxable account it would not be.

This is a behavioural limits-to-arbitrage argument rather than a
balance-sheet one. **It only pays out if the strategy is actually held through
the bad stretch.** If it would be abandoned after two poor years, the edge does
not exist for this account and the project should not proceed.

---

## 2. What counts as success

The benchmark is **buy-and-hold SPY with identical monthly contributions**, not
zero. This is the single most important difference from the PEAD project, whose
gate only asked "is expectancy positive".

Two claims are registered separately, because they are likely to disagree:

| Claim | Test | Prior |
|---|---|---|
| **A — raw return** | CAGR > SPY CAGR, net of costs | Hard. Cycle-dependent. |
| **B — risk-adjusted** | Sharpe > SPY Sharpe **and** max drawdown ≤ 0.75× SPY's | More achievable |

"Materially lower drawdown" is defined as **at most three-quarters of SPY's
maximum drawdown over the same window** — so a period in which SPY falls 50%
requires this strategy to fall no more than 37.5%. Fixing the threshold now
prevents a 48% drawdown being described as an improvement later.

**The stated goal is claim A.** It is registered here that claim A is the
harder bar and may fail in a period where claim B succeeds — "similar return,
half the drawdown" is the most likely outcome. Whether that constitutes success
is the account owner's decision, recorded before the result is known so it
cannot be rationalised afterwards.

---

## 3. Universe

**Eleven GICS sector ETFs plus three defensive assets**, US-listed for testing.

| Ticker | Exposure | Data from |
|---|---|---|
| XLK | Technology | Dec 1998 |
| XLF | Financials | Dec 1998 |
| XLV | Health Care | Dec 1998 |
| XLY | Consumer Discretionary | Dec 1998 |
| XLP | Consumer Staples | Dec 1998 |
| XLE | Energy | Dec 1998 |
| XLI | Industrials | Dec 1998 |
| XLB | Materials | Dec 1998 |
| XLU | Utilities | Dec 1998 |
| XLRE | Real Estate | Oct 2015 |
| XLC | Communication Services | Jun 2018 |
| GLD | Gold | Nov 2004 |
| IEF | 7–10y Treasuries | Jul 2002 |
| SHY | 1–3y Treasuries (cash proxy) | Jul 2002 |
| SPY | *benchmark only, never held* | Jan 1993 |

### Why broad sectors and not themes

The first draft of this design proposed a thematic universe — semiconductors,
AI, robotics, data centres. **That was wrong, and for the same reason the PEAD
universe needed a point-in-time screener.**

- **Survivorship.** Thematic ETFs that failed are delisted and vanish from the
  data. A backtest over surviving themes tests only the themes that worked.
  In 2010 the obvious themes were BRIC, commodities, solar and China; in 2021,
  clean energy, cannabis, space and the metaverse. Selecting today's themes is
  selecting on the outcome.
- **Launch timing.** Issuers launch thematic ETFs *after* a theme is hot,
  because that is when money flows in. Published work on specialised ETFs
  finds material underperformance in the years following launch. The vehicle
  is structurally sold near the peak of its narrative.
- **Testability.** No semiconductor or AI UCITS ETF existed in 2008. The
  strategy could not be tested through the crash that matters most, on the
  assets it would actually trade.

Broad sectors have none of these problems: they existed before, during and
after every crisis in the sample, they are not launched on hype, and they do
not close. **Technology sector exposure (XLK) is how this strategy reaches
semiconductors and AI** — through a vehicle that has existed since 1998 rather
than one created because the theme was already working.

### Point-in-time membership

XLRE (2015) and XLC (2018) did not exist for most of the sample. They enter the
universe **only from their first available month**, never earlier. The existing
`universe_at` pattern from the PEAD work applies directly.

### Testing versus trading

The strategy is **tested** on US-listed ETFs (long history, clean data) and
would be **traded** on LSE-listed, GBP-denominated UCITS equivalents on
Trading 212. This is a stated assumption, not a hidden one:

- The tested series is not the traded series. Tracking difference, different
  fund domiciles and slightly different index construction all introduce error.
- GBP denomination avoids Trading 212's ~0.15% FX **conversion fee**. It does
  **not** remove USD currency exposure — a GBP-listed ETF holding US equities
  still moves with the dollar.
- Mapping US tickers to UCITS equivalents, and confirming each is available on
  Trading 212, is a prerequisite before any live use. It is out of scope for
  the research phase and must not be skipped before it.

---

## 4. Rules

Fully mechanical. No judgement, no discretion, no override.

**Timing**
1. Signals are computed from data through the **last trading day of the month**.
2. Orders are placed on the **first trading day of the following month**.
3. No trading between rebalances, for any reason.

**Selection**
4. Rank every eligible asset by **12-month total return**, defined exactly as:

       return = (adjusted_close[last trading day of prior month]
                 / adjusted_close[last trading day of the month 12 months earlier]) - 1

   Adjusted close, so dividends are included. An asset without a full 12
   months of history is **not eligible** — it cannot be ranked against assets
   that have one, and inventing a shorter lookback for new entrants would give
   XLRE and XLC different rules from everything else.
5. Hold the **top 4**, equally weighted at 25% each.
6. **Absolute filter:** an asset is only held if its own 12-month total return
   is **greater than zero**. A slot that fails this test is filled with IEF
   (bonds); if IEF also fails its own filter, with SHY (cash).
7. Assets not in the top 4 are sold in full.
8. **Ties** are broken by ticker in alphabetical order. Arbitrary, but
   deterministic — two runs of the same backtest must produce identical
   results, and an unspecified tie-break makes that untrue.

**Parameters, and why these values**

| Parameter | Value | Source |
|---|---|---|
| Lookback | 12 months | Convention (Jegadeesh & Titman; Faber) |
| Positions held | 4 | Convention; diversifies across ~1/3 of the universe |
| Rebalance | Monthly | Convention; the standard in the literature |
| Absolute filter | 12m return > 0 | Faber's absolute-momentum rule |

**These are set by convention, not fitted, and are frozen before the first
backtest.** Any later change is a new hypothesis requiring a new spec and a
fresh out-of-sample test. This rule exists because the previous project's
result was a coin flip that would have been trivially "improved" by tuning.

---

## 5. Risk limits

Fixed in advance. None may be relaxed to improve a backtest.

- **Long only.** No shorting, consistent with an ISA.
- **No leverage.** No leveraged or inverse ETFs, at any point.
- **Equal weight.** 25% per slot. No conviction-based sizing.
- **Maximum 4 positions.** Concentration is bounded by construction.
- **No stop losses.** The monthly signal is the exit. A stop would introduce
  an untested second exit rule and intra-month decisions.
- **Defensive slot always available.** When momentum fails, capital goes to
  bonds or cash rather than staying invested.
- **Contributions are invested at the next scheduled rebalance**, never
  intra-month, and never timed.

---

## 6. Data

**Requirement: 20+ years of daily history.** The current Polygon plan holds
five years — roughly 60 monthly decisions from a single regime (one inflation
shock, one AI boom). Testing there would produce a number with no
informational content, and a strategy of this type is prone to looking good
over any single favourable stretch.

The sample must cover 2000–02, 2008, 2018, 2020 and 2022 — the periods where
the defensive filter either works or does not. That is the whole question.

**Source: Yahoo Finance chart API**, verified reachable and complete on
2026-08-16:

| Ticker | Range verified | Bars |
|---|---|---|
| XLK, XLE and the seven other original sectors | 1998-12-22 → 2026-08-14 | 6,953 |
| XLRE | 2015-10-08 → 2026-08-14 | 2,726 |
| XLC | 2018-06-19 → 2026-08-14 | 2,050 |
| GLD | 2004-11-18 → 2026-08-14 | 5,468 |
| IEF, SHY | 2002-07-30 → 2026-08-14 | 6,050 |
| SPY (benchmark) | 1993-01-29 → 2026-08-14 | 8,443 |

`adjclose` is present on every series, satisfying the total-return
requirement below. Twenty-seven years of history covering the dot-com crash,
2008, 2018, 2020 and 2022.

**Stooq was the first choice and is unusable.** It now serves a JavaScript
proof-of-work bot challenge rather than CSV, so an HTTP client receives an
HTML page. Defeating it would require a headless browser for a data fetch,
which is not a dependency worth taking.

Requests need a browser `User-Agent` header; without one the endpoint returns
429. The environment's egress allowlist must include
`query1.finance.yahoo.com`.

**Point-in-time handling is unchanged from the existing system.** Each record
carries `known_at`; the replay provider physically cannot serve a record whose
`known_at` is in the simulated future. Monthly signals use only data through
the prior month's close, and this must be enforced structurally rather than by
care.

**Dividends.** Sector ETFs pay meaningful dividends (XLU and XLP especially).
Total-return series are required. Using price-only series would understate
every held position and bias the comparison against SPY in an unpredictable
direction. If the free source provides only price series, adjusted-close data
must be used and the limitation recorded in the manifest.

---

## 7. Evaluation

The existing gate does **not** apply and must not be reused as-is. It requires
200 trades, per-trade R-multiples, a profit factor and a stop-ordering band —
all of which assume per-trade stop-based risk. This strategy has no stops, no
R-multiples, and produces roughly 12 rebalances a year.

**What replaces it**

- **Benchmark-relative.** Every metric is reported against SPY buy-and-hold
  over the identical window with identical contributions.
- **Walk-forward folds.** Four to five consecutive periods, each judged
  separately. The strategy must beat SPY in **at least 3 of 4** folds.
- **Cost stress.** Modelled Trading 212 costs at 1×, 1.5× and 2×. Costs include
  spread on LSE UCITS ETFs and, where relevant, FX conversion.
- **Drawdown.** Maximum drawdown reported alongside return, never omitted.
- **Effective sample size, stated honestly.** 25 years of monthly decisions is
  ~300 observations, but overlapping positions and multi-year regimes reduce
  the *independent* sample to roughly 20–25 annual observations. Error bars
  will be wide enough to contain "no edge", and the report must say so rather
  than present a point estimate as a finding.

**Verdict values:** `OUTPERFORMS` / `NO EDGE` / `INSUFFICIENT DATA`. As with
the previous project, a NO EDGE result is a successful run.

---

## 8. Falsification — what would prove this wrong

Registered before the test, so a bad result cannot be reinterpreted.

1. **Lookback structure.** Momentum should be materially stronger at a 6–12
   month lookback than at 1 month, which historically *reverses*. If a
   1-month lookback performs as well, the result is noise rather than
   momentum, and the hypothesis is wrong.
2. **The defensive filter must earn its keep in crises.** It should add most of
   its value in 2000–02, 2008 and 2022. If it does not help in those periods,
   it is not doing what this design claims and the mechanism is
   misunderstood.
3. **It must underperform somewhere.** If the backtest shows the strategy
   beating SPY in *every* fold — including 2010–2021 — that is evidence of a
   **bug**, most likely look-ahead, not evidence of an edge. This prediction
   is the most useful one in the document: the previous project's first three
   evaluations were all wrong, and each was caught by a number that did not
   reconcile.

---

## 9. Architecture

**A new package inside `quant-desk`**, sharing the parts that are genuinely
general and not forcing the rest.

**Reused unchanged**
- `qd.types` — the two-timestamp rule (`event_time` / `known_at`)
- `qd.clock` — the NYSE calendar
- `qd.providers.replay` — the point-in-time choke point, and its tests
- The walk-forward fold structure and cost-stress idea from `research/`

**Built new**
- Long-history data adapter (Stooq/Yahoo), with its own archive
- Monthly allocation engine: rank → select → apply absolute filter → target
  weights → rebalance orders
- Benchmark-relative evaluator and gate
- A separate `HYPOTHESIS` document recording the registered claims above

**Explicitly not reused:** `Intent`, per-trade stop sizing, `qd.risk`'s
R-multiple machinery, and the existing `EdgeProof` gate. An earlier statement
in this project that "the infrastructure works on ETF bars unchanged" was
wrong and is corrected here.

---

## 10. Honest expected outcome

Sector momentum rotation has historically produced roughly **10–14% CAGR with
20–30% maximum drawdowns**, against an S&P delivering roughly 8–10% with
drawdowns above 50%. The most likely result is therefore **comparable return
with substantially better drawdown** — which satisfies claim B and fails
claim A.

The stated target of 15–20% sits **above** what this strategy type has
delivered historically. It is possible in a favourable stretch and should not
be expected as a durable rate.

The specific failure mode to guard against: a backtest showing 18% CAGR, driven
almost entirely by two enormous years, and read as a repeatable rate. Fold-wise
reporting exists to make that visible instead of averaging it away.

**Most likely verdict, stated in advance: claim A fails, claim B passes.**
Recording that now means a marginal result is read as the coin flip it is
rather than as vindication.
