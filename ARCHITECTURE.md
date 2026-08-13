# Architecture

How the pieces fit, and the reasoning behind the structural choices. For the
risk rules see [RISK.md](RISK.md).

---

## The two-timestamp rule

Every observation in `qd/types.py` carries:

- `event_time` — when the thing happened in the world
- `known_at` — the first moment this system could have acted on it

They are never equal and the gap is never zero.

| Record | `event_time` | `known_at` |
|---|---|---|
| `Bar` | bar start | **bar close** |
| `NewsItem` | wire publication | receipt (later of received/published) |
| `EarningsEvent` | release | **schedule** publication — actuals gated separately |
| `OptionTrade` | SIP timestamp | timestamp + feed latency |

Look-ahead is not a bug you avoid by concentrating. It is the default state of
any code holding a full history, it raises no error, and it makes results
*better*, so nothing about the output invites suspicion. The defence is
therefore structural rather than careful: `ReplayProvider._bound()` is a single
choke point that every read passes through, and it cannot serve a record from
the simulated future. `tests/test_pointintime.py` asserts it, including
monotonicity — advancing the clock reveals strictly more, never less.

**Earnings are the subtle case.** The schedule is public weeks ahead, so
forward-looking requests are legitimate and must not be clamped — the blackout
check needs to see tomorrow's report today. The *actuals* are gated separately
by `EarningsEvent.has_actuals_at()`. Clamping the provider would break the
blackout; not gating the actuals would hand the backtest the EPS before the
release.

---

## Data flow

```
providers ──> features ──> Evidence ──> strategy ──> Intent ──> risk ──> Order ──> broker
                              │             ▲          │                    │
                              │         context        └────── journal ─────┘
                              │        (layer 1,
                              │         hard gate)
```

**Layer 1 — context** (`qd/context.py`) classifies regime and volatility state
from daily bars, using two crude measures with fixed thresholds, and gates
everything downstream. It runs *before* any signal reasoning: checking it after
conviction would let a strong signal in a regime the strategy was never
measured in reach the sizing engine.

Its thresholds are deliberately not exposed in config. A regime filter tuned
against returns is a second strategy hiding inside the filter — with its own
overfitting and its own need for proof — and when the system loses money you
cannot tell which of the two failed.

Regime derives from daily bars, so it can only change once a day; the engine
caches it per symbol per trading day.

`Evidence` is the common currency. Every channel — bars, headlines, earnings,
options prints — reduces to `(source, kind, score ∈ [-1,1], confidence ∈ [0,1],
observed_at, ttl, detail)`. The strategy layer combines them without knowing
anything about their origins, which is what lets a channel be added, reweighted
or removed without touching decision logic.

`detail` carries the raw numbers behind each score. It is not decoration: when
a trade goes wrong, the journal replays `detail` to show which specific input
produced the conviction.

### Aggregation is two-stage, and the order matters

```
within a source   confidence-weighted MEAN
across sources    weighted SUM
```

Summing within a source would let the market channel — which emits four
readings — outvote news, which emits one. That is a counting artefact, not a
finding. Averaging first makes each channel one vote whose strength is its own
internal agreement.
(`test_many_weak_market_readings_do_not_outvote_one_strong_channel`)

### Decay

`Evidence.decayed_score()` applies exponential decay with a half-life of TTL/3,
then hard-expires at TTL. Information does not stay fresh until a cliff and then
vanish; it bleeds out continuously. A breaking headline is worth far less on its
second hour than its first.

---

## The engine loop

`Engine.cycle()`, once per `poll_interval`:

1. roll the trading day if the calendar moved
2. refresh bars, news, earnings, options tape
3. rebuild evidence for every symbol
4. **watchdog** — flag stale data
5. **manage open positions** — exits, partials, breakeven, time stops
6. assess candidates → size → submit
7. journal everything, including refusals

**Exits before entries**, unconditionally. Risk reduction outranks risk
addition, and a stop that needs moving must not queue behind a scan of forty
symbols. Exits run even while entries are halted.

**The engine holds no vendor knowledge.** It receives a `Providers` bundle and
cannot tell live from replay. That is what makes the backtest a test of *this
code* rather than of a parallel implementation that resembles it — the single
most common reason backtests fail to predict live behaviour.

---

## Clock injection

Nothing calls `datetime.now()` inside a decision path. `Clock` is a protocol
with `LiveClock` and `SimClock`; `now` is an argument everywhere.

A provider or feature that reads the wall clock cannot be replayed, because
replay works by lying about the time. `SimClock` also refuses to move backwards
— a replay loop that steps back re-serves consumed records, producing duplicate
signals and an inflated trade count that looks like a busy, profitable system.

---

## Simulated execution

`qd/providers/sim.py` is deliberately pessimistic. The ways a simulator can
flatter a strategy are numerous, subtle, and all point the same direction.

- **Next-bar-open fills.** An order decided on a bar's close fills at the next
  bar's open. Filling at the signal bar's close means trading on the same price
  that generated the signal — enough on its own to make a random strategy look
  profitable.
- **The ordering band.** When one bar contains both stop and target, OHLC
  cannot say which came first. Default assumes the **stop**; `ordering=
  "optimistic"` assumes the target. The truth is inside the band, and
  `research/evaluate.py` rejects any result whose band straddles zero — there
  the assumption, not the data, produced the sign.
- **Costs both ways.** Half-spread plus slippage on entry and exit, plus fees.
  Commission-free is not cost-free; the spread is the commission and it is
  charged twice.
- **Gaps ignore stops.** A gap through the stop fills at the open.

None of this makes the simulation accurate. It makes it biased against the
strategy, which is the only safe bias.

---

## The null test

`research/synthetic.py` generates price paths with no predictable structure —
random returns, with news, earnings and options events sprinkled independently
of what price subsequently does. There is nothing to find.

The evaluator must therefore report **NO EDGE**
(`tests/test_null_hypothesis.py`). This is the most important test in the
repository: a harness that reports an edge on noise is a random number
generator with a confident interface, and every backtest it later produces on
real data is uninterpretable, because there is no evidence it can tell the
difference.

`JudgeTest` also feeds the judge results shaped like each specific way a
backtest flatters itself — profitable only at friendly costs, only in one fold,
only under a favourable ordering assumption — and confirms each is rejected
while a genuinely clean result still passes. A standard that rejects everything
is not a standard.

Deliberately absent from the generator: any mechanism making news predict
returns. Adding one to "check the system finds signal" would be building a
detector for a phenomenon inserted for the detector to find.

---

## The BarSeries invariant

`BarSeries` de-duplicates by bar `end` against the whole series, not just the
newest entry. The engine re-fetches an overlapping window every cycle, so a
series that only checks the last bar accumulates a copy of its entire history
on every pass. The consequences are not cosmetic: volume inflates (feeding RVOL
directly), ATR corrupts, and every append becomes a full re-sort. Asserted in
`tests/test_channels.py::BarSeriesTest`.

---

## Adding a channel

1. Emit `Evidence` from a function in `qd/features/`, with `observed_at` set to
   when the reading became knowable — not when you computed it.
2. Add a weight in `StrategyConfig.weights`.
3. Extend `ReplayDataset` and `ReplayProvider` so it is visible to replay under
   the same `known_at` discipline.
4. Add a look-ahead test.

Step 3 is the one that gets skipped, and skipping it means the channel is live
but invisible to every backtest — so results silently describe a different
system from the one running.

---

## Known gaps

- **No websocket streaming.** Providers are poll-based. Fine at a 20-second
  cadence; wrong for anything latency-sensitive.
- **No earnings provider implemented.** `EarningsSource` is defined and used
  throughout, but no live adapter exists — Finnhub or FMP would slot in.
- **The sector map is static** (`UniverseConfig.sectors`), approximate, and
  will misclassify. A wrong-but-stable grouping still prevents eight
  semiconductor names loading as eight independent bets.
- **Options tape is API-expensive.** A full chain scan across a large universe
  will exhaust a Polygon quota quickly. `contracts()` filters by expiry and
  moneyness before fetching trades; watch the page caps in `_paged()`, because
  a truncated fetch looks downstream like a quiet tape rather than an error.
- **`SimBroker` fills whole orders only.** No partial fills, no queue position.
- **Corporate actions are not handled.** A split mid-backtest will produce
  nonsense unless the data is adjusted upstream.
