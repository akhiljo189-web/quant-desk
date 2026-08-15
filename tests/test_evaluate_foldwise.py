"""
The evaluation must be fold-wise, and each fold must trade its own universe.

The failure this pins was found by audit, in the first full evaluation ever
run: the script handed `evaluate()` the UNION of all five annual screens, and
`evaluate()` had no way to do anything else — `universe_at` existed but only
`walk_forward` accepted it, and `evaluate` never passed it. A name that
qualified only in the 2026 screen was tradeable in 2022. That is the exact
survivorship look-ahead the point-in-time screener was built to remove,
reintroduced at the final step, and it flatters the verdict.

A full-span run cannot re-screen its universe mid-run, so every full-span
number is either union (look-ahead) or frozen-at-start (stale). The only
honest evaluation is built from per-fold runs, each trading the universe that
had been screened by its own start date.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock

from qd.config import Mode, Settings
from qd.providers.replay import ReplayDataset
from qd.types import UTC
from research.replay import ReplayResult, universe_at, walk_forward


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


UNIVERSES = {
    "2022-01-05": ["AAA", "BBB"],
    "2023-01-05": ["CCC"],
    "2024-01-05": ["DDD", "EEE"],
}


class UniverseAtTest(unittest.TestCase):
    def test_picks_the_latest_screen_already_selected(self):
        self.assertEqual(universe_at(UNIVERSES, ts("2023-06-01")), ("CCC",))

    def test_a_later_screen_is_never_visible_earlier(self):
        """The 2024 screen knows who survived 2023. It must not exist for a
        fold that starts in 2022."""
        self.assertEqual(universe_at(UNIVERSES, ts("2022-04-01")), ("AAA", "BBB"))

    def test_before_the_first_screen_there_is_no_universe(self):
        self.assertEqual(universe_at(UNIVERSES, ts("2021-06-01")), ())

    def test_the_screen_date_itself_counts(self):
        self.assertEqual(universe_at(UNIVERSES, ts("2024-01-05")), ("DDD", "EEE"))


class WalkForwardUniverseTest(unittest.TestCase):
    def capture_runs(self, **wf_kwargs):
        calls = []

        def fake_run(settings, dataset, start, end, **kwargs):
            calls.append((tuple(settings.universe.symbols),
                          kwargs.get("journal_path"), start, end))
            return ReplayResult(start=start, end=end)

        s = Settings.load(Mode.REPLAY)
        with mock.patch("research.replay.run", fake_run):
            walk_forward(s, ReplayDataset(), ts("2022-04-01"), ts("2026-04-01"),
                         folds=4, **wf_kwargs)
        return calls

    def test_each_fold_trades_the_universe_screened_by_its_start(self):
        calls = self.capture_runs(universes=UNIVERSES,
                                  journal_path="data/t.jsonl")
        got = [c[0] for c in calls]
        self.assertEqual(got[0], ("AAA", "BBB"))     # fold 1 starts 2022-04
        self.assertEqual(got[1], ("CCC",))           # fold 2 starts 2023-04
        self.assertEqual(got[2], ("DDD", "EEE"))     # fold 3 starts 2024-04
        self.assertEqual(got[3], ("DDD", "EEE"))     # fold 4: no newer screen

    def test_each_fold_gets_its_own_journal(self):
        """One shared file makes fold N's blocked_reasons include folds
        1..N-1 — the diagnostics stop being per-fold at all."""
        calls = self.capture_runs(universes=UNIVERSES,
                                  journal_path="data/t.jsonl")
        paths = [c[1] for c in calls]
        self.assertEqual(len(set(paths)), len(paths), paths)


class EvaluateThreadsUniversesTest(unittest.TestCase):
    def test_every_pass_receives_the_universes(self):
        """The cost sweep, the optimistic band and the consistency folds are
        all measurements of the same strategy; any of them running the union
        universe is the same leak through a different door."""
        from research import evaluate as ev

        seen = []

        def fake_walk_forward(settings, dataset, start, end, folds=4,
                              universes=None, **kwargs):
            seen.append((universes, kwargs.get("cost_mult"),
                         kwargs.get("ordering")))
            return [ReplayResult(start=start, end=end,
                                 cost_mult=kwargs.get("cost_mult", 1.0),
                                 ordering=kwargs.get("ordering", "worst"))]

        s = Settings.load(Mode.REPLAY)
        with mock.patch.object(ev, "walk_forward", fake_walk_forward):
            result = ev.evaluate(s, ReplayDataset(), ts("2022-04-01"),
                                 ts("2026-04-01"), universes=UNIVERSES)
        self.assertTrue(seen)
        for universes, _, _ in seen:
            self.assertIs(universes, UNIVERSES)
        # No trades in the stubs, so the verdict must be the honest one.
        self.assertEqual(result.verdict, "INSUFFICIENT DATA")


class CombineTest(unittest.TestCase):
    def fold(self, curve, cycles=10, ambiguous=1, blocked=None):
        r = ReplayResult(start=curve[0][0], end=curve[-1][0],
                         cycles=cycles, ambiguous_bars=ambiguous)
        r.equity_curve = list(curve)
        r.blocked = dict(blocked or {})
        return r

    def test_equity_is_stitched_multiplicatively(self):
        """Each fold restarts at the same equity; read literally that erases
        every fold's outcome from the next fold's drawdown. Stitching scales
        each fold to start where the previous one ended, so two losing folds
        compound instead of resetting."""
        from research.evaluate import combine
        t = ts("2022-04-01")
        a = self.fold([(t, 100_000.0), (t + timedelta(days=1), 110_000.0)])
        b = self.fold([(t + timedelta(days=2), 100_000.0),
                       (t + timedelta(days=3), 90_000.0)])
        out = combine([a, b])
        self.assertAlmostEqual(out.equity_curve[-1][1], 99_000.0)
        self.assertAlmostEqual(out.max_drawdown_pct(), 10.0, places=6)

    def test_counts_and_blockers_merge(self):
        from research.evaluate import combine
        t = ts("2022-04-01")
        a = self.fold([(t, 100_000.0), (t + timedelta(days=1), 100_000.0)],
                      cycles=5, ambiguous=2, blocked={"x": 3})
        b = self.fold([(t + timedelta(days=2), 100_000.0),
                       (t + timedelta(days=3), 100_000.0)],
                      cycles=7, ambiguous=1, blocked={"x": 1, "y": 4})
        out = combine([a, b])
        self.assertEqual(out.cycles, 12)
        self.assertEqual(out.ambiguous_bars, 3)
        self.assertEqual(out.blocked, {"x": 4, "y": 4})
        self.assertEqual(out.start, a.start)
        self.assertEqual(out.end, b.end)

    def test_empty_input_is_an_empty_result(self):
        from research.evaluate import combine
        self.assertEqual(combine([]).count, 0)


class FreshJournalTest(unittest.TestCase):
    def test_fresh_truncates_what_a_previous_run_left(self):
        """Replay journals are per-run artefacts. Appending across runs means
        every rerun inherits the previous run's counts."""
        from qd.journal import Journal
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        Journal(path).event("leftover")
        self.assertGreater(os.path.getsize(path), 0)
        Journal(path, fresh=True)
        self.assertEqual(os.path.getsize(path), 0)

    def test_default_still_appends(self):
        """The live journal must never truncate — it is the audit trail."""
        from qd.journal import Journal
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        Journal(path).event("one")
        Journal(path).event("two")
        with open(path) as fh:
            self.assertEqual(len(fh.readlines()), 2)


if __name__ == "__main__":
    unittest.main()
