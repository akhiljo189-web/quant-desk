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

---

## 13. MEASURED — 2026-09-06: what is actually in the Form 4 stream

The Form 4 parser (`qd/providers/forms.py`) was run against real EDGAR filings
for the first time. This section records what came back, because one number in
it changes §7 and §8.

**Sample.** 240 Form 4 documents, the most recent 30 each for INTC, F, MU, AMD,
WDC, ON, MCHP and GT. 959 transaction rows. Zero fetch failures, zero parse
failures.

### Composition

| Rows | Share | Classification |
|---|---|---|
| 380 | 39.6% | Rule 10b5-1 plan |
| 272 | 28.4% | derivative table row |
| 257 | 26.8% | compensation (codes A, F, M) |
| 42 | 4.4% | open-market sale |
| 6 | 0.6% | transfer (gift, conversion) |
| **2** | **0.2%** | **open-market purchase** |

Two purchases in 959 rows:

- 2026-06-24 — F, THORNTON JOHN L, director, 10,600 sh, $148,880
- 2026-08-14 — INTC, TAN LIP BU, CEO, 105,263 sh, $9,999,985

### What this changes

**§1 understated the problem.** The hypothesis section says "the overwhelming
majority" of Form 4s are automatic, and §4's module note says roughly nine in
ten. The measured figure is **999 in 1000**. The exclusion machinery is not a
refinement on the signal; it is essentially the whole computation, and a parser
without it would be reporting a number in which the true signal is 0.2% of the
mass.

**§8's sample problem is worse than stated, and in a specific way.** The
concern registered there was the number of independent *time* periods. This
adds a second, sharper constraint: in any 90-day window, almost every company
in a universe will have **no qualifying purchase at all** and will score
exactly zero. A decile sort on the insider score would therefore be sorting
mostly ties, and the "top decile" would not be the top 10% of a distribution —
it would be whichever handful of companies had any insider buying at all,
padded out with zeros.

**Consequence for §7, registered before any result.** The primary test must
therefore NOT be a decile sort on the raw score. It is an **event study**:
companies with a qualifying purchase in the window, against a matched control
set drawn from the same universe, sector and size band with no purchase in the
same window. The decile sort is retained only as a secondary reading *within*
the set that has any activity, where the score is actually distinguishing
between non-zero values. Registering this now, before any return data has been
touched, prevents the more flattering framing being chosen after the fact.

This does not weaken the hypothesis. Insider purchases being rare is part of
why they are informative — a signal present in every name every quarter would
be a factor, not information. But it means the achievable sample is much
smaller than the universe size suggests, and the error bars in §8 should be
expected to be wider still.

### What was validated, and what was not

**Validated.** Every exclusion path fires on real data: the structured 10b5-1
flag, the derivative/non-derivative split, and each compensation code. The
double-count trap is visible in the sample — a code M option exercise appears
once in each table on the same filing, and only the derivative row is marked as
such.

**Not validated.** The footnote-prose path for Rule 10b5-1. A separate run
checked how many documents were flagged by prose alone, with no structured
`aff10b5One` flag: **zero of 240**. Every plan exclusion in this sample came
from the structured flag. That means the deliberately over-broad
document-scoped prose rule (`forms.py`, `_has_plan_prose`) cost nothing here —
but it also means the path was never exercised, because the SEC only mandated
the structured flag in 2023 and every filing in this sample post-dates it.

The prose path is what covers the pre-2023 filings, which is most of the
history any long-horizon backtest would use. **It remains untested, and testing
it requires pulling filings from before 2023 — which `filings.recent` may not
reach.** This is a known gap, recorded rather than assumed away.

---

## 14. AMENDMENT — 2026-09-06: the stage-0 gate has a free path

§3 states that without a delisting-inclusive universe source the project stops,
and §6 lists EDGAR reconstruction as "partial mitigation at best". The account
owner has confirmed no CRSP/WRDS access. Before accepting a paid subscription
as the only route, the free route was tested rather than assumed.

**It works, for the universe.** EDGAR submissions were pulled for seven
companies known to have died — Sears Holdings, Bed Bath & Beyond, Lehman
Brothers, Silicon Valley Bank, First Republic, Blockbuster — alongside two live
controls. Every one returned a full filing history with a readable last-filing
date. A company that ever filed is in EDGAR permanently; vendors drop it, the
SEC does not. **The survivorship-free universe is free.**

Three cautions the test surfaced, which shape how death must be detected:

1. **"Last filing" is not the delisting date.** Lehman Brothers' most recent
   filing is dated 2025 — bankruptcy estates and post-bankruptcy shells keep
   filing for years. The death signal must be the last **periodic** report
   (10-K/10-Q), or better, the **Form 25** (delisting notification) and
   **Form 15** (deregistration) that are the actual legal markers.
2. **`filings.recent` truncates at ~1000 entries.** Heavy filers lose their
   early history, exactly as `edgar.recent_only_warning` already reports for
   the PEAD universe. Older filings need the separate archive files.
3. **Filing patterns differ by entity type.** First Republic returned 43 forms
   and no 10-K in the window; bank holding companies file differently. Any
   rule keyed on 10-K presence alone will mislabel a whole sector.

### What is still missing, and why it is no longer fatal

EDGAR gives the universe and the approximate date of death. It does not give
the **terminal return** — what a holder actually lost between the last
observable price and the delisting. That gap was the reason §3 called this
"partial mitigation".

The gap is real but it is **bounded and testable**, which is different from
unknown. The registered approach:

- Assign an explicit delisting return to every name that dies, following the
  established convention for missing delisting data (Shumway 1997), and record
  the assumption in the archive manifest rather than burying it in code.
- **Run every result at three assumptions: 0%, −30% and −100%.** The −100% case
  is the pessimal bound — every delisted holding went to zero.
- **The sensitivity IS the finding.** If the verdict is stable across that
  range, the missing prices do not matter and the paid data would not have
  changed the conclusion. If the verdict flips somewhere inside it, that is
  positive proof the paid data is required, which is a far better basis for
  spending four figures than a prior.

This is deliberately the opposite of the usual order. Rather than buying data
to avoid a bias, the bias is bounded first, and the purchase happens only if
the bound turns out to be load-bearing.

**Registered before any return data is touched:** a result that survives the
−100% assumption is the only one that may be reported without the sensitivity
table attached. Anything weaker must be reported with all three numbers, and a
result that holds only at 0% is to be read as no result at all.

### Stage 0 is therefore unblocked, with its limitation recorded

The gate in §11 is satisfied by the free path, not by a purchase. §3's "the
project stops here" no longer applies. What it is replaced by is narrower and
must not be overstated: the universe is survivorship-free, the death dates are
approximate, the terminal returns are assumed rather than measured, and every
downstream result carries the three-way sensitivity or it does not get reported.

---

## 15. CORRECTION — 2026-09-06: −100% is not a conservative bound

§14 called the −100% delisting assumption "the pessimal bound". **That is
wrong, and the error is not cosmetic.** It is recorded here rather than edited
away, because the reasoning that produced it is the reasoning to watch for.

−100% is the pessimal bound **for the portfolio**. It is not a conservative
bound **for the hypothesis test**, because not every company leaves the
universe by failing. Acquisitions, mergers, going-private transactions and
exchange moves all end a listing, and several of them end it at a premium. A
name identified at $40 and acquired two years later at $95 is a large win that
the −100% rule would record as a total loss.

**The direction of the resulting bias depends on the delisting mix, and it can
run either way.** Worse, it can run against the hypothesis in a specific and
undetectable manner: if insider purchases predict *acquisitions* — entirely
plausible, since officers and directors know about merger discussions before
the market does — then marking every acquisition at −100% would systematically
destroy the exact signal being tested. That produces a **false negative**, and
nothing in the sensitivity table as described in §14 would reveal it. A
one-sided robustness check cannot detect a two-sided bias.

### Registered: classify the reason before assigning the return

Every name leaving the universe is classified, and the counts are reported with
every result. The categories:

| Category | Meaning |
|---|---|
| `BANKRUPTCY_DISTRESS` | Chapter 7/11, receivership, FDIC seizure |
| `MERGER_ACQUISITION` | Acquired by another entity |
| `GOING_PRIVATE` | Taken private |
| `EXCHANGE_DELISTING` | Failed a listing standard, not otherwise resolved |
| `UNKNOWN` | Stopped filing, cause unreadable |

The counts are the diagnostic. 143 delistings of which 108 are distress means
delisting-return accuracy is load-bearing and the paid data may be needed. 143
of which 74 are acquisitions means a blanket −100% is absurd and would be
manufacturing a false negative.

### Measured: EDGAR gives the reason as a label, not a heuristic

Tested against five companies known to have died. The markers:

| Marker | Reads as | Reliability |
|---|---|---|
| 8-K **item 1.03** | `BANKRUPTCY_DISTRESS` | **Exact.** Sears 2018-10-15, BBBY 2023-04-24, SVB 2023-03-10, Blockbuster 2010-09-24 — each matches the real Chapter 11 date to the day |
| 8-K **item 3.01** | `EXCHANGE_DELISTING` | Fires, but often *precedes* the real cause — Blockbuster's came ten months before its bankruptcy |
| **SC 13E3** | `GOING_PRIVATE` | Clean, purpose-built form |
| **DEFM14A** + Form 25 | `MERGER_ACQUISITION` | Needs both |
| 8-K **item 2.01** | *not usable alone* | Far too noisy — Sears filed five between 2006 and 2019, nearly all ordinary asset sales rather than the company being acquired |

Item 1.03 has the same property that makes item 2.02 trustworthy in
`providers/edgar.py`: the filer applies the code under a legal obligation, so
identifying a bankruptcy is a **label lookup rather than a guess**.

**Ordering rule.** Markers appear in sequence and the first is not the cause —
Blockbuster's exchange-delisting notice precedes its bankruptcy by ten months.
Classification takes the terminal cluster and resolves by priority:
`BANKRUPTCY_DISTRESS` > `GOING_PRIVATE` > `MERGER_ACQUISITION` >
`EXCHANGE_DELISTING` > `UNKNOWN`.

### The blind spot, which is worse than it looks

**First Republic produced no death markers at all.** It was seized by the FDIC
and sold to JPMorgan; the holding company never filed Chapter 11, so item 1.03
never fired.

Bank failures follow receivership rather than bankruptcy, and they will land in
`UNKNOWN`. **Banks are precisely the sector where distress clusters**, so the
blind spot is not randomly distributed — it removes some of the worst outcomes
from exactly the industry that produces the worst outcomes, and it removes them
in the flattering direction.

Registered consequence: **the `UNKNOWN` bucket is assigned the distress
treatment, not the neutral one.** If `UNKNOWN` exceeds 15% of delistings, the
result is reported as `INSUFFICIENT DATA` regardless of what the returns say.

### Registered: the verdict tree

Applied after the sensitivity table, before any interpretation:

| Verdict | Condition | Consequence |
|---|---|---|
| 🟢 **Robust** | Passes at 0%, −30% **and** −100% | Delisting returns are not driving the result. CRSP unnecessary |
| 🟡 **Data-sensitive** | Passes at 0% and −30%, fails at −100% | Assumptions materially affect the verdict. CRSP/WRDS becomes worth its cost |
| 🟠 **Fragile** | Passes only at 0% | **No edge may be claimed.** Missing terminal returns matter too much |
| 🔴 **Fail** | Fails at 0% | Stop. CRSP cannot rescue the hypothesis |

With the §15 correction attached: a 🟢 result whose delistings are mostly
acquisitions is **stronger** than the table implies, since −100% understated
those names. A 🟠 result whose delistings are mostly distress is exactly as bad
as it looks.

### Registered: build order, and the separation that matters

```
13D/G parser → 13F parser → universe reconstruction
    → point-in-time validation → FREEZE DATASET → only then compute returns
```

The freeze is the load-bearing step. **No decision about how the historical
universe is constructed may be made while any strategy performance number is
visible.** Every remaining degree of freedom — how death is dated, how
`UNKNOWN` is treated, which delisting marker wins — is settled and frozen
first. Choosing those rules with a return number on screen is the most
comfortable form of overfitting available to this project, and the one least
likely to feel like cheating at the time.

---

## 16. MEASURED — 2026-09-06: the 13D/G parser against real filings

`qd/providers/schedules.py` was validated against 120 Schedule 13D/G filings
sampled across 2026 Q3 from the EDGAR form index. **120 parsed, 0 empty, 0
fetch failures, and percent-of-class read on 120 of 120 positions.**

### The group double-count, measured

| | Count |
|---|---|
| Reporting-person rows | 301 |
| Distinct positions after `collapse_group` | 120 |
| **Inflation if summed naively** | **2.51×** |

Reporting persons per filing ran 1, 2, 3 … up to **21 on a single filing**.
A fund files jointly with its general partner, its manager and its principal,
and all of them report the same block because all of them beneficially own it.

This is not a rounding problem. A "multiple institutions accumulating the same
stock" signal — which the source brief asks for explicitly and §5.3 weights —
would have been **two and a half times overstated on average**, manufactured
entirely out of a filing convention. `collapse_group` keys on the accession,
because a joint filing is by definition one submission, and keeps the largest
reported stake within each group.

### Composition of the sample

| Split | Counts |
|---|---|
| Form | 104 × 13G, 16 × 13D |
| Filer class | 83 institutional (13d-1(b)), 16 passive (13d-1(c)), 5 exempt (13d-1(d)), 16 activist |
| Stake | min 0.00%, median 6.30%, max 68.33% |
| Exits (amendment below 5%) | **26 of 120 — 22%** |

Two things follow. **13G outnumbers 13D roughly seven to one**, so pooling them
would drown the informative form in the passive one — the design's separation
of the two is load-bearing, not tidiness. And **more than a fifth of filings
are holders leaving**, arriving on an identically-shaped document; storing only
the percentage would let every one of them read as an ordinary disclosure. The
`is_exit` flag exists for that and the measured 22% is why.

### Traps found in the schemas, all now pinned by tests

Verified by reading real 13D and 13G documents rather than assuming a shared
format. The two forms describe the same fact and agree on almost no field name:

| | 13D | 13G |
|---|---|---|
| namespace | `.../schedule13D` | `.../schedule13g` |
| event date | `dateOfEvent` | `eventDateRequiresFilingThisStatement` |
| issuer CIK | `issuerCIK` | `issuerCik` |
| percent | `percentOfClass` | `classPercent` |
| person block | `reportingPersons/reportingPersonInfo` | `coverPageHeaderReportingPersonDetails` |
| holder CIK | present | **absent entirely** |

The namespaces differ only in the case of the final letter. A parser matching
fully-qualified tags handles one form and returns an empty list for the other,
which does not look like a bug — it looks like a company with no large holders.
Everything matches on local element names for that reason.

Two further consequences recorded: dates are **MM/DD/YYYY**, not Form 4's ISO,
so a value like 03/04/2026 would silently transpose month and day if read as
ISO. And 13G carries **no holder CIK at all**, so holders can only be matched
across filings by name — which is fuzzy, and is a known limit on any
"same institution accumulating over time" measure.

### The disclosure lag is read, not assumed

13G's deadline depends on the filer's class, which the filing states in the
rule it cites: 13d-1(b) institutional, 13d-1(c) passive, 13d-1(d) exempt. The
class parses on every filing in the sample. The 2023 amendments also shortened
these deadlines, so any single fixed lag would be wrong on one side of that
date whichever value it took; what is stored is the maximum lag by class, used
to bound staleness rather than to date anything — `known_at` does the dating.

### The limit that matters most

Filings before the structured-XML mandate are HTML or plain text with no
`primary_doc.xml`, and there is no reliable way to read a percentage from two
decades of free-form cover pages. Those return an empty list.

**That is correct and it is dangerous**, because an empty list is
indistinguishable from "this company had no 5% holders". `has_structured_data`
exists so a caller can tell the two apart, and any archive built from these
must record which era it covers. Structured documents were confirmed present
across 2024 Q1 through 2026 Q3; how far back they run is not yet established
and must be before the universe reconstruction depends on them.

---

## 17. GATE DG-HISTORY-001 — missing is not zero

**Registered as a hard pre-reconstruction gate, on reviewer instruction.**

> Historical universe or backtest construction must not interpret the absence
> of structured Schedule 13D/G XML before **2024-12-18** as absence of
> beneficial ownership.
>
> Before historical scoring uses that period, either
> **(A)** legacy HTML/text 13D/G parsing is implemented and validated, or
> **(B)** the affected signal is marked unavailable and handled explicitly,
> never scored zero.

### The boundary is regulatory, not empirical

§16 noted structured filings present from 2024 Q1 and left the true start
"not yet established". That framing was wrong and would have wasted a research
cycle: the boundary is set by rule, and sampling could never have found it.

| Period | Treatment |
|---|---|
| Before 2023-12-18 | Legacy HTML/ASCII |
| 2023-12-18 → 2024-12-17 | **Mixed.** XML optional — its presence proves nothing about completeness |
| 2024-12-18 onward | Structured XML required, subject to exceptions |

A sample can only ever show that structured filings **exist** in a period,
never that they are **complete** in it. The middle row is the trap: observing
XML in early 2024 and concluding coverage begins there would silently treat
every legacy filing in that year as an absence of holders.

### Why this is the most dangerous bias in the project so far

The error is **time-dependent**, which is the worst kind. Institutional
ownership would appear to rise out of nothing at the exact moment the file
format changed — a step change in the Big Money Score with no economic cause,
perfectly correlated with a date. Any model would learn it.

`qd/providers/schedules.py` now returns a `ScheduleParseResult` carrying a
`CoverageState`, never a bare list:

| State | Meaning | Scoreable as zero? |
|---|---|---|
| `PARSED_STRUCTURED` | XML read, records returned | — |
| `PARSED_LEGACY` | legacy text read (not yet built) | — |
| `LEGACY_UNPARSED` | pre-mandate, not machine-readable | **NO** |
| `PARSE_FAILED` | structured but malformed | **NO** |
| `NO_RELEVANT_POSITION` | read fine, nothing to report | **yes** |

Only `NO_RELEVANT_POSITION` returns true from `is_evidence_of_absence`.

Option (B) is sufficient for a first result on recent data. It is **not**
sufficient for the 10–20 year history the source brief's NVIDIA/AMD/Micron
framing implies — that needs option (A), a legacy parser, and the build order
below reflects it.

---

## 18. Registered — permanent invariants and the revised build order

### INVARIANT BREADTH-001

> One Schedule 13D/G accession contributes **at most one** ownership-position
> event to breadth or convergence metrics, unless the filing explicitly
> contains economically distinct positions.

Permanent. Implemented as `breadth_events` and pinned by test. "Economically
distinct" is deliberately narrow: reporting persons on the same accession
holding **different share counts**. Same count, same filing, one position.

Validated on 100 real filings: 244 reporting-person rows collapse to 135
breadth events, and every one of the 17 accessions contributing more than one
event carries genuinely distinct holdings (one example: 446,759 / 49,242 /
441,294 shares on a single filing). The invariant permits the exception it
states and nothing else.

### 13D and 13G are never pooled

The forms carry different information and arrive in a 7:1 volume ratio (§16).
Registered: **13G volume must not overpower 13D information simply because
there are more filings.** 13D is a declaration of control intent and carries
the higher weight per filing; 13G is ownership evidence and carries less.
Any scoring that sums them into one "5% owner" count is prohibited.

### Position changes, not snapshots

A stake at 7.2% means nothing without its predecessor. `classify_change`
returns `NEW_POSITION`, `INCREASE`, `UNCHANGED`, `DECREASE`, `EXIT`,
`BELOW_5_PERCENT` or `UNKNOWN_CHANGE`. Two rules worth stating:

- An **amendment with no prior on file is `UNKNOWN_CHANGE`, never
  `NEW_POSITION`** — calling it new would invert the sign every time the
  amendment is in fact a reduction.
- `BELOW_5_PERCENT` is distinct from `EXIT`: the holder is still there, but
  the next filing may never come.

### Holder identity — false negatives preferred

13G carries no holder CIK, so longitudinal matching falls back to names, and
"BlackRock Fund Advisors", "BlackRock Institutional Trust Company" and
"BlackRock, Inc." are related but economically distinct entities. Every record
carries `holder_name_raw`, `holder_name_normalized`, `holder_entity_id` and
`holder_identity_confidence`.

`same_holder` requires **HIGH confidence — a CIK match — by default.**
Normalisation strips legal-form wrappers only and never distinguishing words,
so the three BlackRock entities above normalise to three different strings, by
test. Relaxing the bar to MEDIUM is a deliberate act visible at the call site.

A missed increase costs sample. An invented one costs the result.

### Revised build order

```
13F parser → validate 13F → historical universe reconstruction
   → LEGACY 13D/G PARSER (GATE DG-HISTORY-001)
   → point-in-time validation → FREEZE DATASET → returns
```

The legacy parser moves ahead of the freeze rather than being optional,
because historical Big Money scoring cannot be run without it under
DG-HISTORY-001 option (A), and option (B) will not carry a 20-year study.
