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
| Universe | 30–50 mid-caps | where coverage is thin enough for drift to survive | annual point-in-time screens in `research/universes/annual.json` (built by `research/screen.py`); each walk-forward fold trades the screen dated before its own start, and live runs refuse a screen older than ~13 months |
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

## Amendment — 2026-08-14: the surprise measure

The hypothesis is unchanged. The way the TRIGGER measures surprise has
changed, and that is a material enough alteration to record here rather than
edit quietly into the code. A pre-registration that can be revised whenever
the data turns out inconvenient is decoration.

**What forced it.** The plan was analyst-consensus surprise from Finnhub. Its
free tier returns **four quarters**. Across a 35-name universe that is ~140
earnings events before the regime filter, confluence rule and liquidity gates
take their cut — against a gate demanding 200 out-of-sample trades. Any result
from four quarters would be noise wearing a decimal point.

**What replaced it.** Time-series SUE — standardised unexpected earnings — from
SEC XBRL:

    SUE = (EPS_q − EPS_{q−4}) / stdev(past seasonal differences)

The expectation is the same quarter one year earlier (a seasonal random walk),
and the scaling is the company's own history of surprises. Seven-plus years of
EPS, free, from the source of record.

**This is the original methodology, not a workaround.** Ball & Brown (1968) and
Bernard & Thomas (1989) established PEAD using time-series SUE precisely
because analyst forecasts were not broadly available. Drift is documented under
both definitions.

**But they are not the same measurement**, and the difference is worth stating:

| | Consensus surprise | Time-series SUE |
|---|---|---|
| Expectation | what analysts predicted | the company's own seasonal trend |
| Captures | information analysts hold | information history holds |
| Blind to | history the analysts ignore | anything analysts knew that history did not |

A company that grows steadily produces small time-series SUE and can still
badly miss consensus. The two disagree most for names with heavy analyst
coverage — which, by the hypothesis's own logic, are the names we care about
least.

**Validation, measured — 2026-08-14.** Sign agreement between time-series SUE
and Finnhub consensus surprise, over 31 overlapping quarters across 12 names:

| Both measures decisive above | Agreement |
|---|---|
| (all observations) | 20/31 = **65%** |
| \|SUE\|>0.5 and \|consensus\|>2% | 10/15 = **67%** |
| \|SUE\|>1.0 and \|consensus\|>5% | 9/14 = **64%** |

**Agreement does not improve as both measures become decisive.** That is the
result that matters. Had the disagreements been noise clustered near zero,
filtering to confident readings would have pushed agreement sharply up; it is
flat instead, so the two measures genuinely disagree about a third of the time
even when both are sure.

The cause is structural rather than a defect. Companies beat consensus roughly
three times in four — guidance is managed downward into the print — so
consensus surprise is systematically POSITIVE, while SUE is symmetric around
zero by construction. Against those base rates, chance agreement is about 50%;
65% is a positive but weak relationship, which is what two genuinely different
measurements of the same event look like.

**The consequence, stated before the result is known:** this is not "the same
signal, free". We are testing a DIFFERENT strategy from the consensus-surprise
version. If the walk-forward reports NO EDGE, that is evidence about
time-series-SUE PEAD in mid-caps — it is **not** evidence that
consensus-surprise PEAD fails, and it must not be reported as though it were.
Conversely a positive result does not transfer to the consensus definition
without re-testing.

The implementation itself is not in doubt: the AAPL and TXRH figures reconcile
against reported EPS once split adjustment and Q4 derivation are applied, and
the three XBRL traps documented in `qd/providers/xbrl.py` were each found and
fixed by this validation.

**A correction to this amendment — the winsorizing claim was wrong.** The text
above, and the code comment it described, said that capping SUE at ±4 defended
against a one-off item contaminating the year-ago comparison. It does not, and
the failure is arithmetic rather than a matter of tuning. A lone shock inflates
the very denominator used to judge it: with *n* prior seasonal differences, a
single outlier of **any** magnitude scores exactly `n/√(n−1)` — 3.6 at n=12,
3.3 at n=20. It is always under the cap, whatever the writedown was. Multiply
CROX's −8.82 by a hundred and the score does not move.

So the cap fires on companies whose surprises are broadly volatile and never on
the case it was written for. The artefact is now identified by its shape
instead: an extreme seasonal difference of the **opposite sign exactly one year
earlier** is the signature of a rebound off a one-off rather than an earnings
surprise. Those readings are flagged and dropped from the trigger rather than
merely sized down — they arrive wearing maximum conviction, which is the worst
possible way to be wrong. Both behaviours are pinned by tests
(`test_winsorizing_never_binds_on_a_lone_shock`,
`test_a_rebound_off_a_one_off_is_flagged_contaminated`).

The same pass found two more, both of which had been producing plausible
numbers throughout the validation above:

- **The scale leaked.** Prior differences were ordered by period END, but a
  derived Q4 carries its 10-K's filing date, so a quarter that ended earlier
  can enter the public record later. The scale now filters on filing date.

- **The fiscal labels were wrong.** XBRL's `fy`/`fp` describe the FILING a fact
  came from, not the period it covers. A 10-K restates the prior year's
  quarters, so those rows arrive tagged `fp="FY"` with March, June and
  September end dates. TXRH had fifteen quarters labelled `FY` ending across
  four different months — pairing on that label compares a Q3 against a Q1,
  which is the exact wrong-quarter failure the label matching was written to
  prevent, arriving through a different door. Labels are now derived from the
  period end against the company's own fiscal year end, which also handles
  52/53-week calendars where a "December" year end lands on 2 January.

After the fix the quarter counts are balanced (TXRH 17/17/16/16 across
Q1–Q4, sixteen years) and the figures reconcile: AAPL FY2024 reads
2.18 / 1.53 / 1.40 / 0.97, the last being the European State Aid tax
charge — which is itself a live example of the contamination case, since
FY2025 Q4 will measure against it.

**What this does not fix.** XBRL cannot look forward, so there is no scheduled
earnings calendar. Backtests are unaffected — every filing is in the past — but
the live "do not hold into a print" blackout falls back to estimating the next
report from the company's filing cadence. That degradation is explicit in
`qd/providers/edgar.py::estimate_next_report` and must be treated as an
estimate: being early costs a skipped trade, being late means holding through
a gap.

**If the result is marginal or positive**, consensus surprise becomes worth
$99/month — not as an upgrade taken on faith, but as a re-run over the
overlapping years to measure whether the definition changes the answer.

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
