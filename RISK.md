# Risk contract

The rules the system will not break, why each exists, and what it costs.

Written before results are known, which is the only time such a document is
worth anything. Every limit here will at some point block a trade that would
have worked, and that moment — not this one — is when a risk rule is actually
tested.

---

## 1. Sizing is risk-first

```
quantity = (equity × risk_pct) / |entry − stop|
```

Size falls out of the stop distance. It is never derived from a notional
target, a conviction score, or available buying power.

**Conviction does not affect size.** A 0.99-conviction signal is sized
identically to a 0.36 one. Conviction is an opinion produced by this system's
own scoring; the stop distance is a measurement of the market. Sizing up on
conviction means one confident wrong idea does the damage of five ordinary
ones — and nothing in this system verifies that its confidence correlates with
being right.

**Wider stops mean smaller positions.** This is the correct direction of the
trade-off. A tight stop buys more size, which is exactly why it is tempting and
exactly why ordinary intraday noise removes the position before the thesis has
a chance.

Asserted by `tests/test_risk.py::test_conviction_does_not_change_size`.

---

## 2. Caps only ever reduce

| Cap | Default | What it protects against |
|---|---|---|
| Position notional | 20% of equity | single-name concentration |
| Sector notional | 35% of equity | eight semis are one bet, not eight |
| Gross exposure | 1.5× equity | total leverage |
| Net exposure | 1.0× equity | directional bet size |
| Open positions | 8 | attention and correlation |
| Positions per sector | 3 | as above |
| New positions per day | 5 | overtrading on a busy news day |
| **Total open risk** | **2.0% of equity** | **the one that matters** |

Total open risk — the sum of every position's distance to its stop — is the
number that answers "if today goes badly, how badly". Position count and gross
notional both understate what a correlated move costs.

No cap can raise the risk-derived size. If a cap cuts size below one tradeable
share, the trade is **rejected**, not squeezed in: a position too small to
matter still pays full costs and still occupies a slot and a share of the day's
risk budget.

Net exposure is directional — a short added to a long book *reduces* net
exposure and must not be blocked by it
(`test_net_exposure_does_not_block_an_offsetting_trade`).

---

## 3. Circuit breakers

| Breaker | Default | Reset |
|---|---|---|
| Daily realised loss | −2.0% | next session |
| Weekly realised loss | −4.0% | Monday |
| Consecutive losses (day) | 4 | next session |
| Consecutive losses (week) | 8 | Monday |

Realised only. Unrealised swings are noise, and a breaker that trips on them
fires during the ordinary wobble of a position that is working.

Percentage limits and streak limits are separate tests. The percentage catches
one bad position sized correctly; the streak catches a signal that has stopped
working — six small losses in a row is information even when the total is
modest.

---

## 4. The stop is at the broker, always

Every order is submitted as a bracket: entry, protective stop and target,
atomically. A stop enforced inside this process is not a stop — it is an
intention that survives exactly as long as the process, the machine and the
network do. Positions outlive all three.

On startup, `reconcile()` reads the broker's positions and open orders. A
position with no stop leg is the worst possible state, and the response is to
**halt trading and say so loudly**, not to trade around it.

`client_order_id` is derived deterministically from the intent, bucketed to the
minute. A restart that re-derives the same intent reuses the same ID and the
broker rejects the duplicate — the difference between a crash costing nothing
and a crash silently doubling a position.

---

## 5. Gaps are where stops stop working

A stop is an order resting against a market. Between 16:00 and 09:30 there is
no market for it to rest against, and it fills at the opening print, wherever
that lands.

- **Earnings blackout**: no entries within 24h of a scheduled report, and no
  holding through one. The distribution is bimodal and the stop does not exist
  across the gap — this is a coin flip at the size of a considered trade. The
  strategy enters *after* a release; the blackout guards the **next** one,
  which is a different event, not ours.
- **Overnight holding is the strategy.** PEAD is a multi-day drift, and
  overnight gap exposure is precisely the risk the hypothesis says the market
  pays for carrying. The overnight cap therefore matches the position cap
  (8) instead of force-flattening the book nightly — the binding constraint on
  gap damage is the 2% total-open-risk ceiling, which caps what a correlated
  gap-down costs regardless of how many names are held.
- **Post-release settling**: no entries for 30 minutes after a print, while the
  spread is enormous and the first prints are unreliable.
- **Drift-window exit**: every position is closed unconditionally 8 days after
  entry, winners included. Past the window the position is no longer held
  because of the event, and an open-ended hold is an unregistered momentum bet.

The simulator models this: a gap through the stop fills at the open, not the
stop price (`test_gap_through_the_stop_fills_at_the_open_not_the_stop`).

---

## 6. Pattern Day Trader rule

Under $25,000 equity, a US margin account gets 3 day trades per rolling **five
business days**. Breaching it freezes the account for 90 days, which ends the
experiment regardless of P&L. Enforced as a hard constraint.

The check is conservative — it blocks at the limit rather than after it,
because a position opened now may need to be closed today, and discovering the
breach at exit time is too late. The window counts business days, not calendar
days: over a weekend a calendar window silently drops two sessions and lets the
count reset early.

---

## 7. Stale data halts entries, never exits

If the feeds stop updating, the system is trading against a frozen picture
while the market moves. This is the one state where doing nothing strictly
dominates, and the system cannot notice from the inside — every number still
computes.

Entries halt; exits, stop moves and position management continue. A halt is a
reason to *reduce* risk, never to sit on it.

The watchdog does not fire in the first minutes of a session, when the newest
bar is legitimately yesterday's. An alarm that cries wolf every morning at
09:30 is one nobody reads on the day it is real.

---

## 8. Session timing

- No entries in the first **5 minutes** — the opening auction unwinds into the
  first prints, spreads are wide, and the tape is not yet describing the day.
- No entries in the last **20 minutes** — a position opened there cannot reach
  its target before the close, so it becomes an unplanned overnight hold.
- Extended hours **off** by default. Tradeable is not the same as sensible: a
  stop that is sane at noon is noise at 04:30.

---

## 9. The go-live gate

Real money requires all five, checked before **every** live order:

1. An edge proof from `research/evaluate.py`, hash-intact.
2. ≥ 200 out-of-sample trades.
3. Positive expectancy at **2× modelled costs**.
4. Positive across ≥ 60% of walk-forward folds.
5. ≤ 30 days old, and a paper run of ≥ 20 days / 30 trades that does not
   badly trail the backtest.

Clearing this bar does not mean the system is good. It means it is not
obviously broken.

The gate is checked per order rather than at startup because a process that
passed in March must not still be trading on that verdict in September. The
proof is hash-stamped so that editing a number invalidates it — tamper-evident,
not tamper-proof. Anyone with repository access can regenerate a proof saying
anything; the hash defends against quietly nudging a threshold after a
disappointing result, not against a determined author.

**If you are editing `qd/gate.py` to get past it, that is the signal it was
written to give you.**

---

## What this does not protect against

Stated plainly, because a risk document that only lists its strengths is
marketing.

- **A wrong signal.** Every rule here bounds the size of a loss; none makes the
  strategy correct. Perfect risk management on a negative-expectancy signal
  produces a slow, orderly decline.
- **Correlation you did not model.** The sector map is static and approximate.
  In a real selloff, correlations converge on 1 and the sector caps stop
  meaning what they meant in the backtest.
- **Broker or venue failure.** Bracket orders assume the broker honours them.
  Halts, LULD bands and outages all break that assumption.
- **A gap larger than the position.** The overnight caps bound how many
  positions are exposed, not how far a stock can gap.
- **Cost model error.** Costs are modelled pessimistically and swept to 2×.
  Reality is not obliged to stay inside that.
