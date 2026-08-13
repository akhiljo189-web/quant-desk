# Edge hypothesis

This document should have existed before any code in this repository. It did
not, and that is the central flaw in what was built first: four evidence
channels were engineered, tested and documented without anyone writing down
what inefficiency they were supposed to exploit.

The test applied below has three clauses, and the third is the one that kills
things:

1. What inefficiency exists?
2. Who is on the other side of it?
3. **Why has it not been arbitraged away?**

A channel that cannot answer clause 3 is not an edge. It is a feature.

---

## The four channels, tested

### News classification — FAILS

> *Inefficiency:* headlines carry information that moves prices, and the move
> takes time to complete.

**Who is on the other side?** Nobody, because we are not in the trade. That is
the whole finding.

**Why hasn't it been arbitraged away?** It has. Machine-readable news —
Bloomberg B-PIPE, Refinitiv/Reuters News Analytics, Dow Jones Newswires — is
delivered to co-located subscribers with sub-millisecond parsing, and
sentiment-scored news products have been commercial since roughly 2008. The
first move on a guidance cut is complete before a REST poll returns.

This system polls a news endpoint on a 20-second cadence. It is not competing
in that race; it is reading the scoreboard afterwards. "AI reads the news
faster" was never the claim, but "AI reads the news" is not one either — the
NLP is table stakes and has been for over a decade.

**Verdict: not a signal.** Demote to veto and context only — knowing that a
guidance cut landed 40 minutes ago is a reason *not* to be long into the drift,
which is a use that does not require winning a latency race.

### Options flow — MOSTLY FAILS

> *Inefficiency:* large, urgent, opening options positions reveal a directional
> view held by someone better informed.

**Who is on the other side?** The market maker who was just lifted. And that is
the problem: the mechanism by which informed option buying moves the underlying
IS the dealer's delta hedge, and the dealer hedges in milliseconds. The price
impact of the information is complete before the print is queryable.

**Why hasn't it been arbitraged away?** Largely, it has. Pan & Poteshman (2006)
found option volume predicts returns; the effect has been documented as
concentrating in very short horizons and decaying substantially as dealer
hedging automated through the 2010s. An entire retail industry now sells
"unusual options activity" alerts, which is itself evidence that whatever
remains is thoroughly mined.

This system aggregates a 30-minute window from a polled REST endpoint. Even if
residual predictive content exists at multi-day horizons for genuinely large
opening positions, that is precisely the slice the paid vendors have combed.

**Verdict: not a primary signal.** Retain as confirmation — the module's real
value is that it is honest about what it cannot sign, and its filters (straddle
detection, opening-vs-closing, quote-relative aggressor) are the parts that
would matter if it were ever used for anything.

### Market structure — NOT AN EDGE CLAIM

Trend alignment, relative volume, VWAP extension. These were never proposed as
inefficiencies and none is one. They are context and confirmation, which is the
correct role, and the prior repository's research log already documented that
intraday breakout, fade and trend-continuation all died net of costs on honest
testing.

**Verdict: keep as filter, never as reason.**

### Post-earnings announcement drift — WEAKLY SURVIVES

> *Inefficiency:* prices continue to drift in the direction of an earnings
> surprise for days to weeks after the release, rather than repricing at once.

**Who is on the other side?** Slow-repricing flow: retail holders, index and
target-date funds that rebalance on a schedule rather than on news, and
institutions constrained from taking concentrated single-name positions
immediately after a print.

**Why hasn't it been arbitraged away?** This is the only one of the four with a
real answer, and it is a limits-to-arbitrage story rather than an
information-speed one:

- Closing the gap requires holding concentrated **idiosyncratic** risk through
  a period of elevated realised volatility. Levered arbitrageurs are capital-
  constrained from doing that at size.
- The short leg carries borrow cost and recall risk.
- The drift is largest where transaction costs and capacity limits bite hardest
  — small and mid caps with thin analyst coverage.

Documented since Ball & Brown (1968) and Bernard & Thomas (1989), and still
present in later work, though **substantially decayed**. Anyone claiming
1980s-magnitude PEAD today is quoting a number that no longer exists.

Critically, this survives clause 3 *because it is not a speed race*. A
multi-day holding period is a game where co-location confers no advantage — the
constraint that keeps the anomaly alive is balance-sheet and mandate, not
latency, and we are not competing on either.

**Verdict: the only defensible hypothesis available from what was built.**

---

## The tension this creates — and it is a real one

PEAD is strongest where liquidity is worst.

The effect concentrates in names with low analyst coverage and slow information
diffusion: small and mid caps. The liquidity filter that makes a strategy
safely tradeable — the $20M average dollar volume floor in
`UniverseConfig` — screens out exactly the names where the inefficiency is
largest.

The 24 symbols currently configured (AAPL, MSFT, NVDA, ...) are the most
heavily covered, most efficiently priced equities in the world. **They are the
worst possible universe for this hypothesis.** Every sell-side desk models
their quarters; drift there is closest to zero.

So the universe choice is a direct trade of safety against edge, and it has to
be made deliberately rather than inherited from a default list:

| Universe | Liquidity | Expected drift | Honest read |
|---|---|---|---|
| Mega-cap (current) | excellent | ~zero | safe and pointless |
| Mid-cap $2–20B | good | small but real | the actual candidate |
| Small-cap < $2B | poor | largest | untradeable at retail cost |

---

## The hypothesis, in one sentence

> Post-earnings announcement drift persists in **mid-cap US equities**
> (roughly $2–20B, below top-tier sell-side coverage) because closing it
> requires holding concentrated idiosyncratic risk through a high-volatility
> window that capital-constrained arbitrageurs cannot take at size; the other
> side is index and retail flow that reprices over days rather than seconds.

Falsifiable, names a counterparty, and gives a persistence mechanism that is
not "we are faster".

**What follows mechanically from it:**

| Decision | Value | Why the hypothesis forces it | Enforced |
|---|---|---|---|
| Horizon | 2–10 day hold | drift plays out over days; intraday bars measure noise | `max_hold=8d` unconditional, `time_stop=3d` for flat trades |
| Universe | 30–50 mid-caps | where coverage is thin enough for drift to survive | 35-name $2–20B list in `UniverseConfig` (a snapshot — regenerate from a screener) |
| Trigger | earnings surprise + confirmation | the event IS the hypothesis | `trigger_sources=(EARNINGS,)`, required; sign sets direction |
| News role | veto / classification only | cannot win the speed race; can avoid a known-bad hold | only opposing news readings are consulted; agreement is discarded |
| Flow role | confirmation only | already arbitraged at the horizons it is visible on | confirms scale conviction, capped at the trigger's strength |
| Entry timing | day after release, not the print | the gap is not the drift, and is untradeable | 30-min post-release settling + PEAD evidence only after actuals' `known_at` |

Each row's enforcement has a test. The one that matters most:
`test_news_and_flow_alone_cannot_trade` — evidence that traded under the old
symmetric blend must never trade again.

**What it predicts, that would falsify it:** drift should be *monotonically
weaker* with market cap and analyst coverage. If a backtest shows equal or
stronger drift in mega-caps than mid-caps, the hypothesis is wrong and the
result is a data artefact — because the whole persistence argument rests on
coverage being the binding constraint.

That is a genuine out-of-sample prediction about the *structure* of the result,
not just its sign, and it is worth more than the headline expectancy number.

---

## Honest expected outcome

**This will probably not clear the gate.** PEAD has decayed for forty years, is
taught in every quant curriculum, and the accessible mid-cap slice is the part
institutions can most easily reach. The realistic distribution of outcomes is
weighted heavily toward "positive gross, zero-to-negative net of costs".

That is a perfectly good result to establish, and it is why
`research/evaluate.py` is built to return NO EDGE. But it should be stated
before the test rather than discovered after it, so that a marginal result is
read as the coin-flip it is rather than as vindication.

The failure mode to watch: finding a small positive expectancy, deciding the
cost model is "too conservative", and relaxing it. The cost model is the
hypothesis's toughest opponent and it should stay that way.
