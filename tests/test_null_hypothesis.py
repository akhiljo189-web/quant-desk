"""
The null test: the evaluator must find NO EDGE in random data.

This is the most important test in the repository, and it is the one that
protects every other result. A research harness that reports an edge on noise
is not a measuring instrument — it is a random number generator with a
confident interface, and every plausible backtest it later produces on real
data is uninterpretable, because there is no evidence the tool can tell the
difference between signal and nothing.

So the harness is tested the way you would test a smoke detector: not by
checking that it stays quiet in a quiet room, but by confirming it can fire —
and then confirming it stays silent when there is genuinely nothing there.

Two layers:

  END TO END   Synthetic data with no predictable structure, run through the
               real engine. The verdict must not be EDGE.

  THE JUDGE     Fabricated results shaped like each specific way a backtest
               flatters itself — profitable only at friendly costs, profitable
               only in one period, profitable only if you assume the target
               filled before the stop. Each must be rejected, and a genuinely
               clean result must still pass, so the standard is demonstrably
               not "reject everything".
"""

from __future__ import annotations

import random
import unittest
from datetime import date, datetime, timedelta

from qd.config import Mode, Settings
from qd.gate import DEFAULT_REQUIREMENTS, Requirements
from qd.portfolio import ClosedTrade
from qd.types import Side, UTC
from research.evaluate import _judge
from research.replay import ReplayResult

START = datetime(2024, 1, 2, 14, 30, tzinfo=UTC)


def trades(n: int, mean_r: float, sd_r: float = 0.6, seed: int = 3) -> list[ClosedTrade]:
    """Synthetic closed trades whose mean R is EXACTLY `mean_r`.

    Re-centred rather than merely drawn from N(mean, sd). Sampling error at
    n=100 is ~0.06R, which is the same size as the effects these tests are
    asserting about — so an un-centred fixture makes the test measure the
    random seed rather than the judge.
    """
    rng = random.Random(seed)
    draws = [rng.gauss(0.0, sd_r) for _ in range(n)]
    shift = mean_r - (sum(draws) / n)
    out = []
    for i, d in enumerate(draws):
        r = d + shift
        risk = 100.0
        out.append(ClosedTrade(
            symbol="X", side=Side.BUY, quantity=100,
            entry_price=100.0, exit_price=100.0 + r,
            opened_at=START + timedelta(hours=i),
            closed_at=START + timedelta(hours=i + 1),
            pnl=r * risk, r_multiple=r,
        ))
    return out


def result(
    n=400, mean_r=0.10, cost_mult=1.0, seed=3, ambiguous=0, ordering="worst",
) -> ReplayResult:
    r = ReplayResult(
        trades=trades(n, mean_r, seed=seed), start=START,
        end=START + timedelta(days=400), cost_mult=cost_mult, ordering=ordering,
    )
    r.ambiguous_bars = ambiguous
    equity = 100_000.0
    r.equity_curve.append((START, equity))
    for t in r.trades:
        equity += t.pnl
        r.equity_curve.append((t.closed_at, equity))
    return r


class SweepStub:
    def __init__(self, mapping):
        self.results = mapping

    def survives(self, mult):
        r = self.results.get(mult)
        return r is not None and r.expectancy_r() > 0

    def expectancies(self):
        return {str(k): v.expectancy_r() for k, v in self.results.items()}


class JudgeTest(unittest.TestCase):
    """Each test is one way a backtest lies."""

    def setUp(self):
        self.req = DEFAULT_REQUIREMENTS

    def test_a_genuinely_clean_result_passes(self):
        """The standard must be passable, or it is not a standard."""
        base = result(mean_r=0.12)
        sweep = SweepStub({1.0: base, 1.5: result(mean_r=0.09), 2.0: result(mean_r=0.06)})
        folds = [result(n=100, mean_r=0.10, seed=i) for i in range(4)]
        verdict, reasons = _judge(base, sweep, folds, (0.05, 0.18), self.req)
        self.assertEqual(verdict, "EDGE", reasons)

    def test_profitable_only_at_friendly_costs_is_rejected(self):
        base = result(mean_r=0.12)
        sweep = SweepStub({1.0: base, 1.5: result(mean_r=0.02), 2.0: result(mean_r=-0.04)})
        folds = [result(n=100, mean_r=0.10, seed=i) for i in range(4)]
        verdict, reasons = _judge(base, sweep, folds, (0.05, 0.18), self.req)
        self.assertEqual(verdict, "NO EDGE")
        self.assertTrue(any("inside the spread" in r for r in reasons))

    def test_edge_confined_to_one_period_is_rejected(self):
        base = result(mean_r=0.12)
        sweep = SweepStub({1.0: base, 1.5: result(mean_r=0.09), 2.0: result(mean_r=0.06)})
        folds = [
            result(n=100, mean_r=0.80, seed=1),     # one spectacular period
            result(n=100, mean_r=-0.06, seed=2),
            result(n=100, mean_r=-0.05, seed=3),
            result(n=100, mean_r=-0.04, seed=4),
        ]
        verdict, reasons = _judge(base, sweep, folds, (0.05, 0.18), self.req)
        self.assertEqual(verdict, "NO EDGE")
        self.assertTrue(any("lives in specific periods" in r for r in reasons))

    def test_result_that_depends_on_the_ordering_assumption_is_rejected(self):
        base = result(mean_r=0.12)
        sweep = SweepStub({1.0: base, 1.5: result(mean_r=0.09), 2.0: result(mean_r=0.06)})
        folds = [result(n=100, mean_r=0.10, seed=i) for i in range(4)]
        # Pessimistic ordering is negative, optimistic is positive: the sign of
        # the result comes from the assumption, not from the data.
        verdict, reasons = _judge(base, sweep, folds, (-0.05, 0.25), self.req)
        self.assertEqual(verdict, "NO EDGE")
        self.assertTrue(any("spans zero" in r for r in reasons))

    def test_too_few_trades_is_insufficient_not_an_edge(self):
        base = result(n=40, mean_r=0.35)
        sweep = SweepStub({1.0: base, 2.0: base})
        verdict, reasons = _judge(base, sweep, [base], (0.2, 0.5), self.req)
        self.assertEqual(verdict, "INSUFFICIENT DATA")

    def test_too_many_ambiguous_bars_is_rejected(self):
        """If most trades were resolved by the stop-vs-target assumption, the
        bar data is too coarse to measure the strategy at all."""
        base = result(mean_r=0.12, ambiguous=300)   # 300 of 400 trades
        sweep = SweepStub({1.0: base, 1.5: result(mean_r=0.09), 2.0: result(mean_r=0.06)})
        folds = [result(n=100, mean_r=0.10, seed=i) for i in range(4)]
        verdict, reasons = _judge(base, sweep, folds, (0.05, 0.18), self.req)
        self.assertEqual(verdict, "NO EDGE")
        self.assertTrue(any("too coarse" in r for r in reasons))

    def test_zero_expectancy_noise_is_rejected(self):
        """The headline case: 400 trades of pure noise."""
        base = result(n=400, mean_r=0.0, seed=99)
        sweep = SweepStub({
            1.0: base, 1.5: result(n=400, mean_r=-0.02, seed=98),
            2.0: result(n=400, mean_r=-0.05, seed=97),
        })
        folds = [result(n=100, mean_r=0.0, seed=100 + i) for i in range(4)]
        verdict, reasons = _judge(base, sweep, folds, (-0.02, 0.03), self.req)
        self.assertEqual(verdict, "NO EDGE")


class EndToEndNullTest(unittest.TestCase):
    """The real engine, on data with nothing to find."""

    def test_random_data_does_not_produce_an_edge(self):
        import dataclasses
        from research import replay
        from research.synthetic import SyntheticSpec, generate

        s = Settings.load(Mode.REPLAY)
        s = dataclasses.replace(
            s, universe=dataclasses.replace(s.universe, symbols=("AAPL", "MSFT"))
        )
        ds = generate(SyntheticSpec(
            symbols=("AAPL", "MSFT"),
            start=date(2026, 3, 2), end=date(2026, 3, 20),
            seed=5, earnings_every_days=10,
        ))
        r = replay.run(
            s, ds,
            datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
            datetime(2026, 3, 20, 20, 0, tzinfo=UTC),
            journal_path="data/test_null_journal.jsonl",
        )
        # On structureless data the confluence requirement should keep the
        # system out. If this ever starts trading heavily and profitably, the
        # generator has acquired structure or the strategy has acquired a leak.
        self.assertLess(r.expectancy_r(), 0.5)
        if r.count >= DEFAULT_REQUIREMENTS.min_oos_trades:
            self.fail(
                f"random data produced {r.count} trades — investigate before "
                f"trusting any result from this harness"
            )

    def test_replay_is_deterministic(self):
        """Same seed, same result. Without this, a 'improvement' cannot be
        distinguished from run-to-run variance."""
        import dataclasses
        from research import replay
        from research.synthetic import SyntheticSpec, generate

        s = Settings.load(Mode.REPLAY)
        s = dataclasses.replace(
            s, universe=dataclasses.replace(s.universe, symbols=("AAPL",))
        )
        spec = SyntheticSpec(
            symbols=("AAPL",), start=date(2026, 3, 2), end=date(2026, 3, 13), seed=11,
        )
        start = datetime(2026, 3, 2, 14, 30, tzinfo=UTC)
        end = datetime(2026, 3, 13, 20, 0, tzinfo=UTC)

        a = replay.run(s, generate(spec), start, end, journal_path="data/det_a.jsonl")
        b = replay.run(s, generate(spec), start, end, journal_path="data/det_b.jsonl")
        self.assertEqual(a.count, b.count)
        self.assertAlmostEqual(a.expectancy_r(), b.expectancy_r(), places=9)
        self.assertAlmostEqual(a.total_pnl(), b.total_pnl(), places=6)


if __name__ == "__main__":
    unittest.main()
