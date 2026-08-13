# quant-desk

An autonomous US equities trading system that combines four independent
evidence channels — market structure, breaking news, earnings, and the options
tape — under a strict risk envelope, and refuses to touch real money until
walk-forward evidence says the signal works.

```
market data ─┐
news ────────┤
earnings ────┼──> Evidence ──> confluence ──> Intent ──> risk sizing ──> bracket order
options tape ┘     (scored,      (>=2 agree)              (cash-risk        (broker-side
                    dated,                                 first, caps       stop, atomic)
                    attributed)                            only reduce)
```

**Status: paper trading only.** The live path exists and is blocked by
`qd/gate.py` until an edge proof passes. No edge has been demonstrated yet —
see [Honest status](#honest-status).

---

## Why it is built this way

Most of the engineering here is not about finding signal. It is about not
fooling yourself, because the failure modes of this kind of system are
well-known, silent, and all point in the same flattering direction.

**Every record carries two timestamps.** `event_time` is when something
happened; `known_at` is when this system could first have acted on it. A
16:05 earnings release that reaches the feed at 16:05:31 has a `known_at` of
16:05:31, and a backtest that trades it at 16:05:00 is lying. The replay
provider indexes on `known_at` and physically cannot serve a record from the
simulated future — enforced at a single choke point in
`qd/providers/replay.py`, and asserted in `tests/test_pointintime.py`.

**Backtests run the real engine.** `research/replay.py` drives the same
`Engine.cycle()` that trades live, against a simulated clock. The most common
way backtests mislead is not statistical subtlety — it is that the backtest and
the live system are two different programs, and only one of them was tested.

**Confluence over conviction.** At least two independent channels must agree
before anything trades. Each channel fails in its own way — a busted print
inflates volume, an aggregator repeats a headline, a mislabelled spread leg
reads as a directional sweep — and those failures are largely uncorrelated. One
channel screaming is more likely to be that channel breaking than an
opportunity. This costs trades, deliberately.

**Risk-first sizing.** Position size falls out of the stop distance and a fixed
per-trade cash risk — never out of conviction. A 0.99-conviction signal gets the
same cash risk as a 0.36 one, because conviction is an opinion and the stop
distance is a measurement.

**The gate.** Live trading requires a walk-forward proof that stays positive at
2× modelled costs, across a majority of folds, with a real out-of-sample sample
size, recent, and corroborated by a paper run. It is checked before every live
order, not once at startup.

---

## Quick start

No dependencies are needed for the offline path — the core is stdlib-only.

```bash
git clone https://github.com/akhiljo189-web/quant-desk.git
cd quant-desk
python3 -m qd.cli selftest
```

That validates the risk config, builds a synthetic dataset, and runs the full
engine over it. It should report **no trades** — synthetic data has no
structure, so trading it would indicate a leak rather than a discovery.

```bash
python3 -m unittest discover -s tests    # 134 tests
python3 -m qd.cli gate --live            # why live trading is blocked
python3 -m qd.cli replay --symbols AAPL,MSFT --cost 2.0
python3 -m qd.cli journal                # what it did, and what it declined
```

For live data and paper trading:

```bash
pip install -r requirements.txt
cp .env.example .env        # add POLYGON_API_KEY, ALPACA_KEY_ID, ALPACA_SECRET_KEY
python3 -m qd.cli run       # paper by default
```

---

## The four channels

Each reduces to `Evidence` — a directional score in [-1, 1], a confidence, an
observation time, a TTL, and the raw numbers behind it. The strategy layer
combines them without knowing anything about their origins.

### Market structure — `qd/features/market.py`
Trend alignment (EMA separation in ATR units), participation (relative volume
corrected for time of day — 400k shares by 09:45 is extraordinary, the same by
15:45 is a quiet day), extension from VWAP, and opening-gap follow-through.
Weighted highest of the four, not because it predicts best but because it is
the least corruptible: a bar is a bar.

### News — `qd/features/news.py`
A deterministic rule classifier over ~18 event categories with directional
priors, plus hedging detection ("in talks to be acquired" is not "agrees to be
acquired"), negation handling, M&A role resolution (the same headline is
bullish for the target and mildly bearish for the acquirer), source tiering,
and novelty tracking so the tenth syndicated rewrite does not read as ten
independent confirmations.

Deterministic on purpose. A rule classifier is worse than a language model at
reading a headline and better at being backtested: it returns the same label
forever, and it cannot have been trained on data that includes the outcome. To
use a model, run it at ingest and cache the label on the record
(`classify_with`) — never at decision time.

### Earnings — `qd/features/earnings.py`
Two jobs, and the first matters more. **Blackout:** never hold through a
scheduled print — a stop does not fill at its price across a gap, it fills at
the first print, and every risk number elsewhere assumes the stop roughly
holds. **PEAD:** post-earnings drift as one channel among four. When the
reported surprise and the market's own reaction disagree, it follows the
reaction and cuts confidence — the tape saw the whole release, consensus EPS
saw one line.

### Options flow — `qd/features/optionsflow.py`
Built from the raw tape rather than a vendor's pre-scored feed, because a score
you cannot inspect is a score you cannot backtest. Most of this module is
subtraction:

- **Aggressor classification** against the NBBO in force at the trade. Unsigned
  option volume carries no direction — every contract has a buyer and a seller,
  and only the quote says which was in a hurry. Prints inside the spread stay
  unclassified rather than guessed.
- **Sweep detection** — one order shredded across venues in milliseconds.
  Urgency is the signal; patient money works one venue.
- **Multi-leg detection** — near-simultaneous prints with matching sizes are
  classified as vertical / straddle / calendar / risk-reversal. A bought call
  inside a straddle is not bullish, and counting it as bullish is the single
  most common error in retail flow reading.
- **Opening vs closing** — size above prior open interest means new risk.
  Without it, someone exiting a losing bet looks like someone entering a
  confident one.
- **Relative scoring** — against each symbol's own baseline. $2M is a rounding
  error in NVDA and a once-a-year event in a mid-cap; absolute premium mostly
  detects large caps.

Score is `imbalance × unusualness`, multiplied not added: lopsided-but-normal
and huge-but-balanced both score zero.

---

## Risk

Full detail in [RISK.md](RISK.md). The envelope:

| Limit | Default |
|---|---|
| Risk per trade | 0.5% of equity |
| Total open risk | 2.0% |
| Gross / net exposure | 1.5× / 1.0× |
| Single position / sector | 20% / 35% of equity |
| Open positions / per sector | 8 / 3 |
| Daily / weekly loss stop | 2% / 4% realised |
| Loss streak breaker | 4 daily / 8 weekly |
| PDT rule | enforced under $25k |

Every cap can only *reduce* the risk-derived size, never raise it. If a cap
cuts size below what is tradeable, the trade is rejected rather than squeezed
in. Data staleness halts entries but never exits — a halt is a reason to reduce
risk, never to sit on it.

---

## Honest status

- The **infrastructure** is built and tested: 134 tests, including explicit
  look-ahead guards and a null test proving the evaluator reports NO EDGE on
  random data.
- **No edge has been demonstrated.** The scoring weights and priors in
  `qd/config.py` and `qd/features/news.py` are asserted from ordinary market
  knowledge, not fitted. They are round numbers on purpose — anything tuned
  until it looked good would be a curve fit.
- The system currently runs on **synthetic data**, where it correctly does
  nothing. Whether it does anything useful on real data is unknown and requires
  a Polygon subscription and months of paper trading to find out.
- `research/evaluate.py` will happily return **NO EDGE**, and that is a
  successful run. A research tool that cannot return a negative verdict is not
  measuring anything.

Things that would change the picture, roughly in order of value: calibrating
the news priors against realised forward returns; measuring whether the options
flow channel leads price at all on real data; and checking whether the
confluence requirement is filtering noise or just filtering.

---

## Layout

```
qd/
  types.py         records, the two-timestamp rule, Evidence
  clock.py         NYSE calendar (holidays by rule), session phases, sim clock
  config.py        every tunable, loaded from env once
  features/        the four channels
  strategy.py      confluence -> Intent
  risk.py          sizing and the caps
  portfolio.py     positions, exposure, breakers, PDT
  gate.py          the go-live gate
  engine.py        the loop
  journal.py       append-only decision log, including refusals
  providers/       base protocols, polygon, alpaca, replay, sim broker
research/
  replay.py        walk-forward over the real engine
  evaluate.py      cost stress, ordering band, folds -> verdict -> edge proof
  synthetic.py     deterministic fake data for the null test
tests/             134 tests
```

## Licence

MIT
