# Future Leaders / Smart Money Engine — Design

**Status:** awaiting review
**Date:** 2026-09-05
**Source brief:** "Future Leaders Growth / Smart Money Engine" (13pp, supplied
2026-09-05)
**Supersedes:** nothing. PEAD (`HYPOTHESIS.md`) returned NO EDGE on 2026-08-15;
sector momentum (`2026-08-16-sector-momentum-design.md`) is a separate open
hypothesis. This is a third, and it shares less infrastructure than either.

---

## 0. What the brief asks for, and what this document does with it

The brief asks for a system that identifies future market leaders — the next
NVIDIA, AMD, Micron — before the upside is obvious, using growth, TAM, moat,
insider/institutional accumulation, valuation and risk, ranked and categorised,
benchmarked against the S&P 500.

That is a **goal**, not a hypothesis. It names an outcome to want rather than an
inefficiency to exploit, and it does not answer the question this codebase asks
of every project before any code is written: *why is this still available?*

This document does four things:

1. Converts the brief into a hypothesis with a named counterparty and a
   limits-to-arbitrage argument, so it can fail.
2. Separates the parts of the brief that are **measurable point-in-time** from
   the parts that are not. That split is severe: roughly half the brief's
   requested inputs have no honest historical source, and building them anyway
   is how this project would produce a beautiful, fictitious backtest.
3. Pre-registers the scoring model, the success bar and the falsification
   tests before any data is pulled.
4. States the statistical problem — a 3–5 year holding period gives a very
   small independent sample — up front, because it constrains what any result
   here can possibly mean.

Sections marked **[BRIEF, DEFERRED]** record a requirement from the brief that
is deliberately not in version 1, with the reason.

---

## 1. The hypothesis

The brief contains one genuinely exploitable idea, and it is not the growth
screening. It is this:

> **Inefficiency.** Insider purchases and 5%-holder accumulation, disclosed to
> EDGAR on a short statutory lag, predict positive excess returns over the
> following 3–24 months, and the effect is **concentrated in companies below
> meaningful sell-side coverage**. The predictive content is in *purchases* by
> officers and directors, in *clusters*, in names where the buyer's information
> advantage is largest and the audience for the disclosure is smallest.
>
> **Who is on the other side.** Nobody is on the other side of the disclosure —
> that is the point. The other side of the *trade* is index and passive flow
> that does not read filings at all, and discretionary holders selling on
> price weakness while the people who run the business are buying it.
>
> **Why it has not been arbitraged away.** Because the signal is real, public,
> free, well documented in the literature — and expensive to harvest at the
> only size that matters to a fund. Roughly 500,000 Form 4s are filed a year,
> the overwhelming majority of them automatic sales under 10b5-1 plans and
> option exercises, which must be stripped before anything is left. What
> remains sits in companies too small for a large fund to build a position in
> without moving the price, and pays out over quarters rather than weeks. The
> constraint is **capacity and patience**, not information.

This is the same *class* of argument that PEAD used — limits to arbitrage
rather than speed — and it survives the "why not arbitraged" test better,
because there is no latency race at all: the effect is measured over months.

### The honest weaknesses, stated before any result

**One. The brief's framing is selection on the outcome.** NVIDIA, AMD and
Micron are three survivors of a cohort of several hundred semiconductor and
growth names that had similar-looking financials in their pre-breakout years.
Studying what those three had in common finds the properties of *survivors*,
not the properties of *future leaders* — the failures had many of the same
properties and are invisible because they are gone. The brief itself flags
this ("Do NOT use them to fit the scoring system"), and the flag is correct but
insufficient: the risk is not only fitting, it is that the *category* "future
leader" can only be defined retrospectively. Version 1 does not attempt to
predict leaders. It predicts **forward excess return**, which is measurable at
every point in history for every name including the dead ones.

**Two. The insider effect is documented and has decayed.** Lakonishok–Lee
(2001) and the literature after it put the abnormal return to insider purchases
at roughly 4–6% annualised, before costs, in small caps. Post-publication and
post-SOX (which cut the disclosure lag from up to 40 days to 2 business days,
destroying the largest part of the old edge), credible estimates are lower.
Any backtest here reporting a large number is more likely to have a leak than
a discovery.

**Three. The growth/TAM/moat half of the brief is largely untestable.** See
§4. It is not that those factors do not matter — it is that there is no
point-in-time historical record of them, and constructing one from present-day
knowledge is look-ahead by definition. This is the single biggest reduction in
scope between the brief and this design, and it is not negotiable.

**Four. The sample is small, and it is the binding constraint.** See §8.

---

## 2. What counts as success

The benchmark is **SPY total return over the identical window**, as the brief
demands. Not zero.

Three claims, registered separately because they will disagree:

| Claim | Test | Prior |
|---|---|---|
| **A — the signal exists** | Top-decile score portfolio beats bottom-decile by a positive, cost-surviving margin, in a majority of folds | Plausible |
| **B — it beats the index** | Top-decile portfolio CAGR > SPY CAGR net of costs | Hard |
| **C — it is worth the concentration** | B holds *and* Sharpe ≥ SPY's *and* max drawdown ≤ 1.25× SPY's | Harder |

**Claim A is the scientific claim and the priority.** A spread between high and
low scores is evidence about the model. Beating SPY is a different and much
harder question that mixes the model with market beta, sector tilt, and the
particular decade tested.

The brief's own bar — *"Why own this stock instead of the S&P 500?"* — is
claim C. It is registered here that **claim C failing while claim A passes is
the most likely outcome**, and that this would mean the signal is real but not
large enough to justify a concentrated portfolio. That is a legitimate finding,
and it is recorded now so it cannot later be reinterpreted as success.

**The margin that counts as beating.** Claim B requires the top-decile
portfolio to exceed SPY by **at least 2.0% annualised, net of modelled costs at
2×**, in at least 3 of 4 walk-forward folds. A 0.4% edge over a 20-year window
with this sample size is indistinguishable from noise and must not be reported
as an edge.

---

## 3. Universe

**US-listed common equity, point-in-time, delisting-inclusive.**

| Rule | Value | Why |
|---|---|---|
| Listing | NYSE / NASDAQ / NYSE American common stock | Excludes ADRs, funds, trusts, units |
| Market cap | $300M – $20B at each screen date | The band where the coverage argument binds |
| Liquidity | 60-day ADV > $3M | Below this, costs eat any edge |
| Price | > $5 | Excludes the sub-$5 microcap pathologies |
| History | ≥ 8 quarters of filings | Score inputs need a base |
| Delisted names | **retained through their delisting** | Non-negotiable; see below |

**Why not the brief's implied large-cap universe.** The brief's examples are
mega-caps *today*; they were mid-caps when the return happened. The screen must
select on the cap the company had **at the decision date**, which is what
`research/screen.py` already does for the PEAD universe. Extending its band
downward is a parameter change, not new machinery.

### Survivorship is the project's largest single bias risk

The existing archive begins in 2022 and contains only names that existed to be
screened. `research/universes/README.md` is explicit that this narrows
survivorship without removing it, and that the residual points the flattering
way.

For this project that residual is not acceptable, because the hypothesis is
specifically about **small companies, some of which go to zero**. A universe
that quietly drops the failures converts "insider buying predicts returns" into
"insider buying at companies that survived predicts returns", which is true and
worthless.

**Requirement:** the universe source must include delisted securities with
their delisting date and terminal value. This is a hard prerequisite. If no
such source is available within the project's data budget, **the project stops
here** rather than proceeding on a survivor-only universe. Candidate sources
and their costs are enumerated in §6; this is the first thing to resolve and
the plan must not proceed past it.

---

## 4. What is measurable, and what is not

The brief lists roughly 120 inputs. This section sorts them. The test applied
to each is a single question: *could this system have computed this value, from
data it could have held, on the decision date?*

### Tier 1 — measurable point-in-time, from sources already wired

| Brief requirement | Source | Point-in-time stamp |
|---|---|---|
| Revenue / EPS / FCF growth, levels and acceleration | SEC XBRL company facts | `filed` date, first filing only (`qd/providers/xbrl.py`) |
| Gross / operating margin trends | SEC XBRL | same |
| Debt, leverage, interest coverage, cash, burn | SEC XBRL | same |
| Share count → dilution, SBC | SEC XBRL | same |
| Insider purchases/sales, cluster buying | **SEC Form 4** | `acceptanceDateTime` — 2 business days after trade |
| 5% holders, activists, ownership changes | **SEC 13D/13G** | `acceptanceDateTime` — 10 days (13D), varies (13G) |
| Institutional positions, accumulation breadth | **SEC 13F** | `acceptanceDateTime` — **45 days after quarter end** |
| Announcement instants for every filing | EDGAR 8-K item 2.02 | `acceptanceDateTime` (`qd/providers/edgar.py`) |
| Price/volume: DMAs, relative strength, volume anomalies, breakouts | daily bars | bar close |
| P/E, P/S, EV/S, EV/EBITDA, FCF yield | XBRL ÷ price | derived, both sides point-in-time |
| Customer concentration | 10-K risk/segment disclosures | `filed` |
| Return vs SPY, alpha, vol, drawdown, Sharpe, Sortino, capture | bars | bar close |

The EDGAR backbone this depends on **already exists in this repo** and already
enforces the two-timestamp rule. Form 4, 13D/G and 13F are new parsers against
the same submissions API and the same `acceptanceDateTime` field. This is the
strongest reason to build this project here rather than from scratch.

### Tier 2 — measurable, but only with paid point-in-time data

| Brief requirement | Problem | Resolution |
|---|---|---|
| Forward revenue / EPS estimates | Free sources serve **today's** consensus only. Applying it historically is the exact leak class documented in `qd/providers/finnhub.py` — it would make growth "forecasts" perfectly accurate and the backtest spectacular | **Excluded from v1.** Requires point-in-time consensus history (I/B/E/S-class, four figures a year) |
| Analyst estimate revisions | Same | Excluded from v1 |
| Analyst coverage count (the hypothesis's own conditioning variable) | Same source problem | **Proxied** by market cap and ADV, with the proxy named as a weakness |
| Guidance vs actual (management credibility) | Guidance is unstructured text in 8-K exhibits; extraction is a project in itself | [BRIEF, DEFERRED] |

Excluding forward estimates removes two of the brief's growth inputs. That is
a real loss of signal and it is preferred to a fake one.

### Tier 3 — not measurable point-in-time at any price, as specified

| Brief requirement | Why it cannot be scored honestly |
|---|---|
| **TAM / market opportunity** | There is no historical TAM database. Any TAM figure comes from a research note written at a moment in time, is unreliable even then, and is not retrievable as a time series. A TAM score built today encodes what we now know grew — it is look-ahead wearing a spreadsheet |
| Potential future market share, adjacent markets, new categories | Same: these are forecasts, and a backtest that uses today's forecasts is cheating |
| Moat as the brief defines it (brand strength, ecosystem, network effects, customer lock-in) | Not measurable from filings. Scoring them means an analyst opinion formed with hindsight |
| Management credibility, capital-allocation quality | Same |
| Product pipeline, product cycles | Same |
| "Technological disruption" risk | Same |

**What replaces them.** Moat is not dropped; it is **redefined as its
measurable shadow** and the redefinition is stated in the score so nobody later
mistakes it for the brief's richer concept:

- **Gross margin level and stability** — 5-year mean and negative-deviation of
  gross margin. Pricing power leaves a mark here or it does not exist.
- **ROIC persistence** — years in the last 5 with ROIC above cost of capital.
  A moat is, operationally, a return that competition has not competed away.
- **Revenue retention proxy** — revenue volatility relative to sector.
- **R&D and capex intensity** — sustained investment, not a claim about it.
- **Gross margin *trend* under revenue growth** — margins holding or rising
  while revenue compounds is the observable signature of pricing power.
- **Patent grant counts** (USPTO bulk data, dated) — a weak, noisy, *dated*
  measure. Optional; include only if the join cost is low.

The brief says *"Do not give high scores simply because management claims to
have a competitive advantage. Use measurable evidence wherever possible."* This
section is that instruction taken to its conclusion: where no measurable
evidence exists, **the factor is dropped rather than estimated**, and the score
is explicitly a narrower thing than the brief describes.

### The three lags that must be modelled, not assumed

1. **Form 4 — 2 business days.** Good. This is the usable signal.
2. **13D — 10 days; 13G — annual or 45 days after year-end depending on filer
   class.** Usable, with the class distinction respected.
3. **13F — 45 days after quarter end, and it is a *snapshot*, not a
   transaction record.** A position shown in a 13F filed 14 February may have
   been opened on 2 October and closed on 5 January. The brief's "multiple
   institutions accumulating the same stock" is therefore a claim about
   something that was true up to 4.5 months ago. It is included, at low weight,
   as **confirmation only** — never as a trigger. 13F also omits short
   positions, most derivatives, and non-US holdings, so "institutional
   ownership rising" is a partial view by construction.

---

## 5. The scoring model, pre-registered

Registered before any data is pulled, per the brief's own Research Discipline
section. These are a **starting hypothesis to test, not tuned values** — the
same standard as the round-number priors in `qd/config.py`.

### 5.1 Test the factors before testing the blend

The brief proposes an eight-factor weighted score. Eight weights are eight free
parameters, and a combined score that fails tells you nothing about *which*
factor failed. So the order is fixed:

**Step 1 — univariate.** For each factor independently, rank the universe at
each rebalance and measure forward 6/12/24-month excess return by decile. Report
the monotonicity of the decile spread, not just top-minus-bottom: a factor whose
deciles rank in order is a factor; one where only decile 10 is unusual is a tail
artefact.

**Step 2 — correlation.** Factors are not independent. Growth acceleration and
relative strength will correlate strongly; valuation will correlate negatively
with both. Report the correlation matrix before combining anything.

**Step 3 — blend**, at the registered weights below, with **no re-weighting
after seeing step 1**. Re-weighting after seeing the univariate results is
fitting, and it is the specific failure the brief warns against.

Any weight change after step 1 makes the result in-sample and must be labelled
as such in the output.

### 5.2 Opportunity Score (0–100) — registered weights

The brief's proposed weights, adjusted only where a factor was found
unmeasurable in §4, with each adjustment stated:

| Factor | Brief | Registered | Change and why |
|---|---|---|---|
| Revenue / EPS / FCF growth | 20% | **25%** | Absorbs part of the dropped TAM weight; measurable |
| Market opportunity / TAM | 15% | **0%** | Unmeasurable point-in-time (§4 Tier 3) |
| Competitive moat | 15% | **15%** | Retained, redefined as its measurable shadow |
| Growth acceleration | 10% | **20%** | Raised: this is the brief's sharpest idea and it is fully measurable from XBRL. Second difference of YoY growth, not the level |
| Management / execution | 10% | **0%** | Unmeasurable (§4 Tier 3) |
| Big Money accumulation | 15% | **20%** | Raised: this is the hypothesis's core claim and should carry weight proportional to that |
| Relative strength / momentum | 10% | **10%** | Unchanged |
| Valuation | 5% | **10%** | Raised: fully measurable, and 5% is too low to act as the brake the brief describes |

Every factor is scored as a **cross-sectional percentile within the universe at
that date**, never against an absolute threshold. Absolute thresholds embed the
valuation and growth levels of the decade they were written in.

### 5.3 Big Money Score (0–100)

Kept separate and reported separately, as the brief requires.

| Component | Weight | Notes |
|---|---|---|
| Insider purchase intensity (Form 4, open-market buys only) | 35% | 10b5-1 and option-exercise-and-hold **excluded**; this exclusion is most of the work |
| Insider cluster breadth (distinct officers/directors buying in 90 days) | 20% | Cluster is the documented strong form |
| Buyer seniority (CEO/CFO weighted above directors) | 10% | |
| 13D/G accumulation, new 5% crossings | 15% | Activist filings flagged separately |
| 13F breadth change (institutions adding, net of exits) | 10% | Low weight — 45-day-stale snapshot |
| Net insider selling | **negative** 10% | Weak signal in isolation; sales have many innocent reasons |

**The exclusion rule is the module.** The brief's "CEO purchases" line hides the
real work: the majority of Form 4 rows are automatic. Transaction codes must be
respected — code P (open-market purchase) is the signal; codes S, M, A, F and
anything flagged 10b5-1 are not — and getting this wrong turns the score into a
measure of how much stock a company grants its executives.

### 5.4 Risk Score (0–100), separate

Per the brief: **risk is never netted into Opportunity.** Financial risk
(leverage, coverage, burn, runway), dilution (share count growth, SBC as % of
revenue), concentration (customer/segment where disclosed), valuation risk
(multiple percentile, implied growth), and cyclicality (sector-level realised
earnings volatility). All from Tier 1 sources.

### 5.5 Categories and matrix

Categories A–D and the Opportunity × Risk matrix are implemented **as output
formatting from the two scores**, exactly as the brief specifies. They are
presentation, and they carry no research weight until claim A passes.

### 5.6 Entry, exit, divergence [BRIEF, PARTIAL]

- **Entry logic** — the brief's conjunction (score AND risk AND smart money AND
  trend AND relative strength AND valuation) is implemented for v1 as **the
  score plus a single trend filter** (price above 200 DMA). A six-way
  conjunction over a small universe produces almost no trades and therefore no
  measurable sample; each additional condition must earn its place against
  measured results, not be assumed in.
- **Exit logic** — v1 uses a **fixed 12-month hold with quarterly rescoring**,
  exiting when a name leaves the top quintile. The brief's thesis-break exits
  (growth deteriorates, margins contract, big money distributes) are the right
  design and are **deferred to v2**, because each is a discretionary judgement
  that must be defined mechanically before it can be backtested.
- **Smart Money Divergence** — the positive case (price weak, insiders buying,
  fundamentals improving) is implemented in v1 as a flag, and is the most
  interesting single test in the project: it is where the hypothesis is most
  differentiated from plain momentum.

---

## 6. Data

| Need | Source | Status | Cost |
|---|---|---|---|
| Filings index, 8-K/10-K/10-Q instants | EDGAR submissions API | **built** (`qd/providers/edgar.py`) | free |
| Quarterly fundamentals | SEC XBRL companyfacts | **built** (`qd/providers/xbrl.py`) | free |
| Form 4 | EDGAR, ownership XML | **new** | free |
| 13D / 13G | EDGAR | **new** | free |
| 13F-HR | EDGAR, information table XML | **new** | free |
| Daily bars, split/dividend adjusted, **including delisted names** | **unresolved — blocking** | see below | ? |
| Point-in-time universe with delistings | **unresolved — blocking** | see below | ? |
| Forward consensus estimates | I/B/E/S-class | **excluded from v1** | ~$4k/yr |
| Patent grants (optional) | USPTO bulk | new, optional | free |

All EDGAR access inherits the existing 10 req/s limiter and User-Agent
requirement, which are conditions of use rather than suggestions.

### The blocking dependency

Everything on the SEC side is free and largely already built. **The universe
and price history for delisted companies is the one thing this project cannot
get for free, and it cannot proceed without it** (§3).

Options, to be resolved before the implementation plan is written:

1. **CRSP / Compustat** via an academic affiliation — the correct answer if
   available at all.
2. **Sharadar SF1 + Nasdaq Data Link tables** — includes delisted names, low
   four figures a year.
3. **Reconstruct from EDGAR** — every company that ever filed is in EDGAR, and
   a company that stops filing has, with high probability, delisted or been
   acquired. This gives a survivorship-free *universe* for free, but **not
   prices**, so terminal returns for delisted names would still be missing.
   Partial mitigation at best.
4. **Accept a survivor-biased universe** — **rejected**, per §3.

### Point-in-time correctness

The two-timestamp rule (`qd/types.py`) applies unchanged: every fundamental
observation carries the XBRL `filed` date, every filing-derived observation
carries `acceptanceDateTime`, and the replay provider (`qd/providers/replay.py`)
physically cannot serve a record from the simulated future. The restatement
rule from `qd/providers/xbrl.py` — **keep the first filing of each period,
never the corrected one** — applies to every fundamental used here.

One new leak class specific to this project: **the company's own fiscal
calendar changes**, and a company that changes its fiscal year end will produce
period spans that are not three months. The existing span check handles this by
discarding them; that discard must be counted and reported rather than
silently applied, because a systematic discard pattern is itself a bias.

---

## 7. Evaluation

The existing `EdgeProof` gate does **not** apply and must not be reused. It
requires 200 trades, per-trade R-multiples, a profit factor and a stop-ordering
band — all of which assume per-trade stop-based risk. This strategy has no
stops, no R-multiples, and holds for a year or more.

**What replaces it**

- **Decile spread, reported first.** The primary output is forward excess
  return by score decile, with the full decile curve, not just top-minus-bottom.
- **Benchmark-relative.** Every return figure is reported against SPY total
  return over the identical window.
- **Walk-forward folds.** Four consecutive periods, judged separately. The
  registered bar is **3 of 4 folds positive**.
- **Cost stress at 1×, 1.5×, 2×.** Costs must include spread — this universe
  goes down to $300M caps where the spread, not the commission, is the cost.
- **Turnover reported explicitly.** A 12-month hold should produce low
  turnover; if the measured turnover is high, the strategy being tested is not
  the strategy that was designed.
- **Drawdown always reported alongside return.**
- **Null test.** The synthetic-data null (`research/synthetic.py`,
  `tests/test_null_hypothesis.py`) must be extended to this evaluator and must
  report NO EDGE on randomised scores before any real result is believed. The
  existing null test's own history is instructive: it was found vacuous once
  already (commit 3f23ea6) and had to be rewritten.

**Verdict values:** `OUTPERFORMS` / `NO EDGE` / `INSUFFICIENT DATA`. As before,
a NO EDGE result is a successful run.

---

## 8. The sample-size problem, stated plainly

This is the most important paragraph in the document.

The brief asks for a **3–5+ year holding horizon**. Suppose 20 years of history
and 500 names. That sounds like 10,000 name-years, and it is not: cross-sectional
equity returns are dominated by common factors, so the *independent* observations
are closer to the number of non-overlapping time periods — **four to six**, at a
5-year horizon over 20 years. Overlapping windows inflate apparent significance
enormously and are the standard way long-horizon studies fool themselves.

Consequences, accepted in advance:

- **Confidence intervals will be wide enough to contain zero**, and the report
  must print them rather than a point estimate.
- **The primary evaluation horizon is 12 months, not 5 years**, because 12
  months gives roughly 20 non-overlapping periods instead of 4. The 3–5 year
  claim from the brief is reported as a secondary result, explicitly labelled as
  having a sample too small to distinguish from luck.
- **Non-overlapping windows are the headline; overlapping windows may be
  reported only with an explicit variance correction** (Newey–West or a block
  bootstrap) and never as the primary number.
- If the final result rests on two or three exceptional names, the report must
  say so. A fold-wise and contribution-wise breakdown exists to make that
  visible instead of averaging it away.

---

## 9. Falsification — what would prove this wrong

Registered before the test, so a bad result cannot be reinterpreted.

1. **The coverage gradient.** The hypothesis says the insider effect is
   *concentrated in low-coverage names*. Split the universe by size/ADV: the
   effect must be **monotonically stronger in the smaller, less-covered
   band**. If mega-caps show equal or stronger insider alpha, the persistence
   argument is wrong and any positive headline is an artefact. This is a
   prediction about the *structure* of the result and is worth more than the
   headline number — exactly as the cap-gradient test was for PEAD.
2. **Purchases, not sales.** Insider *buys* must carry the signal and insider
   *sells* must be close to uninformative. If sales predict as strongly as
   purchases, the model is picking up something other than insider information
   — most likely a size or liquidity artefact.
3. **Cluster > single.** Multi-insider clusters must outperform single-filer
   buys. If they do not, the "multiple independent signals agreeing" principle
   the brief rests on — and which this codebase's confluence rule also rests on
   — is not operating.
4. **13F must be weak.** A 45-day-stale quarterly snapshot should carry *less*
   signal than a 2-day-old Form 4. If 13F breadth scores as the strongest
   factor, suspect a leak in the quarter-end-to-filing-date join before
   believing it.
5. **It must underperform somewhere.** If the top decile beats the benchmark in
   every fold including 2022, that is evidence of a **bug** — most likely
   survivorship or look-ahead — not evidence of an edge. This prediction has
   already earned its keep once: the PEAD project's first three evaluations were
   all wrong, and each was caught by a number that did not reconcile.
6. **Growth acceleration must beat growth level.** The brief's sharpest claim is
   that accelerating growth outranks steady growth. If the second difference
   adds nothing over the level, that claim is false and 20% of the Opportunity
   Score is measuring nothing.

---

## 10. Architecture

A new package inside `quant-desk`, reusing what is genuinely general.

**Reused unchanged**
- `qd.types` — the two-timestamp rule
- `qd.clock` — the NYSE calendar
- `qd.providers.edgar` — filings index and `acceptanceDateTime`
- `qd.providers.xbrl` — fundamentals with restatement handling
- `qd.providers.replay` — the point-in-time choke point and its tests
- `research/screen.py` — universe screening, with a widened cap band
- The walk-forward fold structure and cost-stress idea in `research/`

**Built new**
- `qd/providers/forms.py` — Form 4 / 13D / 13G / 13F parsers with transaction-
  code handling and the 10b5-1 exclusion
- `qd/fundamentals/` — derived metrics (growth, acceleration, margins, ROIC,
  leverage, dilution) as point-in-time series
- `qd/scoring/` — the three scores, cross-sectional percentile machinery,
  category assignment
- `research/cross_section.py` — decile evaluation, fold-wise, benchmark-relative
- A `HYPOTHESIS`-class document recording the registered claims above and,
  later, the measured result

**Explicitly not reused:** `Intent`, per-trade stop sizing, `qd.risk`'s
R-multiple machinery, `qd.strategy`'s confluence rule (which is built around
intraday evidence with TTLs), and the `EdgeProof` gate.

---

## 11. Staging

Each stage is a stopping point. Later stages are not started until the earlier
one has produced a result.

| Stage | Deliverable | Gate to proceed |
|---|---|---|
| **0** | Resolve the delisting-inclusive data source (§6) | A source exists and is affordable, or the project stops |
| **1** | Form 4 parser + insider score, univariate decile test | Claim A on the insider factor alone |
| **2** | XBRL-derived growth/acceleration/quality factors, univariate | Each factor's decile curve measured and reported |
| **3** | Blended Opportunity/Risk/Big Money scores at registered weights | Claim A on the blend |
| **4** | Portfolio simulation vs SPY, cost-stressed, fold-wise | Claims B and C |
| **5** | Categories, ranking, explanations, portfolio construction | Only after claim A passes |

The brief's instruction — *"Do not optimise position sizing until the
stock-selection model itself has been validated"* — is the reason stage 5 sits
where it does.

---

## 12. Honest expected outcome

The insider-purchase literature, adjusted for post-SOX decay and for costs in a
$300M–$20B universe, suggests a **realistic top-minus-bottom-decile spread of
2–5% annualised before costs, and materially less after**. The most likely
outcome is therefore:

- **Claim A passes weakly** — a real but small decile spread, with confidence
  intervals that touch zero.
- **Claim B is a coin flip**, dominated by which decade the folds happen to
  cover and by sector exposure rather than by stock selection.
- **Claim C fails** — the concentration and drawdown of a 15–25 name portfolio
  of $300M–$20B growth companies will not be repaid by a 2–3% edge.

The growth/TAM/moat half of the brief — the half that makes the system sound
like it can find the next NVIDIA — is the half with the least measurable
support, and stripping it to what is actually measurable is expected to remove
most of its apparent power. **A system that reliably identified future NVIDIAs
from public filings would be the most valuable artefact in finance**, and the
prior that this one does so should be set accordingly.

The specific failure mode to guard against, in order of likelihood:

1. **Survivorship** — a universe missing its failures, producing a large,
   clean, completely fictitious result. This is why §3 is a hard gate.
2. **Forward-estimate leakage** — pulling today's consensus into a historical
   score. This is why forward estimates are excluded rather than approximated.
3. **Reading a 13F snapshot as a transaction** — treating a 4.5-month-old
   position as fresh accumulation.
4. **Re-weighting the score after seeing the univariate results** and reporting
   the outcome as out-of-sample.
5. Finding a small positive spread, deciding the cost model is "too
   conservative", and relaxing it. This one is listed in `README.md` for the
   previous project, and it did not stop being the risk.

**Most likely verdict, stated in advance: claim A passes weakly, claim C
fails.** Recording it now means a marginal result reads as the coin flip it is
rather than as vindication.
