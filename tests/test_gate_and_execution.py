"""The go-live gate, the market calendar, and simulated execution."""

from __future__ import annotations

import dataclasses
import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from qd.clock import CALENDAR, Phase, early_closes, market_holidays
from qd.config import ExecutionConfig, Mode, Settings
from qd.gate import (
    DEFAULT_REQUIREMENTS, EdgeProof, FoldResult, GateResult, PaperRecord,
    Requirements, check,
)
from qd.providers.base import ProviderError
from qd.providers.sim import SimBroker
from qd.types import Bar, Order, Side, UTC, utcnow

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def proof(**kw) -> EdgeProof:
    base = dict(
        generated_at=utcnow() - timedelta(days=3),
        strategy_version="0.1.0",
        data_start="2020-01-01", data_end="2026-01-01",
        oos_trades=400, expectancy_r=0.09, profit_factor=1.25,
        max_drawdown_pct=12.0, cost_mult=1.0,
        stressed={"1.0": 0.09, "1.5": 0.06, "2.0": 0.03},
        folds=tuple(
            FoldResult(f"f{i}", 100, 0.08, 1.2) for i in range(4)
        ),
    )
    base.update(kw)
    p = EdgeProof(**base)
    return dataclasses.replace(p, content_hash=p.compute_hash())


def paper(**kw) -> PaperRecord:
    base = dict(days=30, trades=60, expectancy_r=0.07, profit_factor=1.2)
    base.update(kw)
    return PaperRecord(**base)


class GateTest(unittest.TestCase):
    def test_replay_and_paper_are_always_allowed(self):
        self.assertTrue(check(Mode.REPLAY, None).allowed)
        self.assertTrue(check(Mode.PAPER, None).allowed)

    def test_live_without_a_proof_is_blocked(self):
        r = check(Mode.LIVE, None)
        self.assertFalse(r.allowed)
        self.assertIn("no edge proof", r.reasons[0])

    def test_a_complete_proof_passes(self):
        r = check(Mode.LIVE, proof(), paper())
        self.assertTrue(r.allowed, r.explain())

    def test_edge_that_dies_at_stressed_costs_is_blocked(self):
        r = check(Mode.LIVE, proof(stressed={"1.0": 0.09, "1.5": 0.02, "2.0": -0.01}), paper())
        self.assertFalse(r.allowed)
        self.assertTrue(any("inside the spread" in x for x in r.reasons))

    def test_missing_cost_stress_is_blocked(self):
        r = check(Mode.LIVE, proof(stressed={"1.0": 0.09}), paper())
        self.assertFalse(r.allowed)
        self.assertTrue(any("no result at" in x for x in r.reasons))

    def test_too_few_trades_is_blocked(self):
        r = check(Mode.LIVE, proof(oos_trades=40), paper())
        self.assertFalse(r.allowed)
        self.assertTrue(any("out-of-sample trades" in x for x in r.reasons))

    def test_edge_confined_to_one_period_is_blocked(self):
        folds = (
            FoldResult("f0", 100, 0.60, 3.0),     # all the profit lives here
            FoldResult("f1", 100, -0.05, 0.9),
            FoldResult("f2", 100, -0.04, 0.9),
            FoldResult("f3", 100, -0.03, 0.95),
        )
        r = check(Mode.LIVE, proof(folds=folds), paper())
        self.assertFalse(r.allowed)
        self.assertTrue(any("folds positive" in x for x in r.reasons))

    def test_stale_proof_is_blocked(self):
        r = check(Mode.LIVE, proof(generated_at=utcnow() - timedelta(days=90)), paper())
        self.assertFalse(r.allowed)
        self.assertTrue(any("old" in x for x in r.reasons))

    def test_tampered_proof_is_detected(self):
        good = proof()
        tampered = dataclasses.replace(good, expectancy_r=0.99)  # hash now stale
        r = check(Mode.LIVE, tampered, paper())
        self.assertFalse(r.allowed)
        self.assertTrue(any("hash mismatch" in x for x in r.reasons))

    def test_insufficient_paper_history_is_blocked(self):
        r = check(Mode.LIVE, proof(), paper(days=3))
        self.assertFalse(r.allowed)
        self.assertTrue(any("paper trading" in x for x in r.reasons))

    def test_paper_badly_trailing_the_backtest_is_blocked(self):
        r = check(Mode.LIVE, proof(), paper(expectancy_r=-0.20))
        self.assertFalse(r.allowed)
        self.assertTrue(any("trails backtest" in x for x in r.reasons))

    def test_proof_survives_a_round_trip_to_disk(self):
        p = proof()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "proof.json")
            p.save(path)
            loaded = EdgeProof.load(path)
        self.assertIsNotNone(loaded)
        self.assertTrue(loaded.is_intact())
        self.assertEqual(loaded.oos_trades, p.oos_trades)
        self.assertEqual(len(loaded.folds), len(p.folds))


class CalendarTest(unittest.TestCase):
    def test_2026_holidays(self):
        h = market_holidays(2026)
        self.assertIn(date(2026, 1, 1), h)         # New Year's
        self.assertIn(date(2026, 1, 19), h)        # MLK
        self.assertIn(date(2026, 4, 3), h)         # Good Friday
        self.assertIn(date(2026, 5, 25), h)        # Memorial Day
        self.assertIn(date(2026, 6, 19), h)        # Juneteenth
        self.assertIn(date(2026, 11, 26), h)       # Thanksgiving

    def test_july_4_on_a_saturday_observes_the_friday(self):
        self.assertIn(date(2026, 7, 3), market_holidays(2026))

    def test_new_year_on_a_saturday_is_not_observed(self):
        """The NYSE does not close the preceding Friday — 2022 is the case."""
        self.assertNotIn(date(2021, 12, 31), market_holidays(2021))

    def test_juneteenth_only_from_2022(self):
        self.assertNotIn(date(2021, 6, 18), market_holidays(2021))
        self.assertIn(date(2022, 6, 20), market_holidays(2022))

    def test_half_days(self):
        e = early_closes(2026)
        self.assertIn(date(2026, 11, 27), e)       # day after Thanksgiving
        self.assertIn(date(2026, 12, 24), e)       # Christmas Eve
        self.assertEqual(CALENDAR.session_minutes(date(2026, 11, 27)), 210.0)
        self.assertEqual(CALENDAR.session_minutes(date(2026, 3, 10)), 390.0)

    def test_dst_is_handled_by_the_tz_database(self):
        """A fixed UTC offset would be wrong for half the year."""
        winter = datetime(2026, 1, 20, 14, 35, tzinfo=UTC)   # 09:35 EST
        summer = datetime(2026, 7, 20, 13, 35, tzinfo=UTC)   # 09:35 EDT
        self.assertIs(CALENDAR.phase(winter), Phase.REGULAR)
        self.assertIs(CALENDAR.phase(summer), Phase.REGULAR)

    def test_phases(self):
        d = date(2026, 3, 10)
        self.assertIs(CALENDAR.phase(datetime(2026, 3, 10, 12, 0, tzinfo=UTC)), Phase.PREMARKET)
        self.assertIs(CALENDAR.phase(datetime(2026, 3, 10, 15, 0, tzinfo=UTC)), Phase.REGULAR)
        self.assertIs(CALENDAR.phase(datetime(2026, 3, 10, 21, 30, tzinfo=UTC)), Phase.AFTERHOURS)
        self.assertIs(CALENDAR.phase(datetime(2026, 3, 10, 3, 0, tzinfo=UTC)), Phase.CLOSED)

    def test_trading_day_key_uses_eastern(self):
        """At 21:00 UTC it is tomorrow in London but still today in New York."""
        late = datetime(2026, 3, 10, 23, 30, tzinfo=UTC)
        self.assertEqual(CALENDAR.trading_day_key(late), "2026-03-10")

    def test_elapsed_fraction_spans_the_session(self):
        self.assertAlmostEqual(
            CALENDAR.elapsed_fraction(datetime(2026, 3, 10, 13, 30, tzinfo=UTC)), 0.0, places=3
        )
        self.assertAlmostEqual(
            CALENDAR.elapsed_fraction(datetime(2026, 3, 10, 20, 0, tzinfo=UTC)), 1.0, places=3
        )


def bar(sym, start, o, h, l, c, minutes=5, vol=10_000.0) -> Bar:
    return Bar(sym, start, start + timedelta(minutes=minutes), o, h, l, c, vol)


class SimBrokerTest(unittest.TestCase):
    def setUp(self):
        self.cfg = ExecutionConfig()
        self.broker = SimBroker(100_000.0, self.cfg, cost_mult=1.0, ordering="worst")

    def _order(self, side=Side.BUY, stop=98.0, target=104.0, qty=100, cid="qd-1"):
        return Order("NVDA", side, qty, stop, target, cid)

    def test_entry_fills_at_the_next_bar_open_not_the_signal_close(self):
        self.broker.submit(self._order())
        events = self.broker.on_bar("NVDA", bar("NVDA", NOW, 101.0, 102.0, 100.5, 101.5))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "entry")
        # Filled off the OPEN (101.0) plus costs — never off the close.
        self.assertGreaterEqual(events[0].price, 101.0)
        self.assertLess(events[0].price, 101.2)

    def test_costs_push_the_fill_against_us_on_both_sides(self):
        self.broker.submit(self._order())
        entry = self.broker.on_bar("NVDA", bar("NVDA", NOW, 100.0, 100.5, 99.8, 100.2))[0]
        self.assertGreater(entry.price, 100.0)      # paid up to buy
        ev = self.broker.force_close("NVDA", 100.0, NOW + timedelta(minutes=5))
        self.assertLess(ev.price, 100.0)            # sold down to exit

    def test_duplicate_client_order_id_is_rejected(self):
        self.broker.submit(self._order(cid="qd-dup"))
        with self.assertRaises(ProviderError):
            self.broker.submit(self._order(cid="qd-dup"))

    def test_ambiguous_bar_defaults_to_the_stop(self):
        """When one bar contains both stop and target, OHLC cannot say which
        came first. Assuming the target is the most expensive assumption."""
        self.broker.submit(self._order(stop=98.0, target=104.0))
        self.broker.on_bar("NVDA", bar("NVDA", NOW, 100.0, 100.2, 99.9, 100.1))
        ev = self.broker.on_bar(
            "NVDA", bar("NVDA", NOW + timedelta(minutes=5), 100.0, 105.0, 97.0, 101.0)
        )
        self.assertEqual(ev[0].kind, "stop")
        self.assertTrue(ev[0].ambiguous_bar)
        self.assertEqual(self.broker.ambiguous_bars, 1)

    def test_optimistic_ordering_reports_the_other_end_of_the_band(self):
        b = SimBroker(100_000.0, self.cfg, ordering="optimistic")
        b.submit(self._order())
        b.on_bar("NVDA", bar("NVDA", NOW, 100.0, 100.2, 99.9, 100.1))
        ev = b.on_bar("NVDA", bar("NVDA", NOW + timedelta(minutes=5), 100.0, 105.0, 97.0, 101.0))
        self.assertEqual(ev[0].kind, "target")

    def test_gap_through_the_stop_fills_at_the_open_not_the_stop(self):
        """A stop does not hold across a gap. This is why overnight exposure is
        capped separately from intraday risk."""
        self.broker.submit(self._order(stop=98.0))
        self.broker.on_bar("NVDA", bar("NVDA", NOW, 100.0, 100.2, 99.9, 100.1))
        ev = self.broker.on_bar(
            "NVDA", bar("NVDA", NOW + timedelta(minutes=5), 90.0, 91.0, 89.0, 90.5)
        )
        self.assertEqual(ev[0].kind, "stop")
        self.assertLess(ev[0].price, 92.0)          # nowhere near the 98.00 stop

    def test_limit_order_does_not_fill_when_the_open_gaps_through_it(self):
        self.broker.submit(
            Order("NVDA", Side.BUY, 100, 98.0, 104.0, "qd-lim", limit_price=100.5)
        )
        events = self.broker.on_bar("NVDA", bar("NVDA", NOW, 105.0, 106.0, 104.5, 105.5))
        self.assertEqual(events, [])
        self.assertEqual(len(self.broker.positions()), 0)

    def test_higher_cost_multiple_reduces_pnl(self):
        def run(mult: float) -> float:
            b = SimBroker(100_000.0, self.cfg, cost_mult=mult)
            b.submit(self._order())
            b.on_bar("NVDA", bar("NVDA", NOW, 100.0, 100.5, 99.8, 100.2))
            b.force_close("NVDA", 102.0, NOW + timedelta(minutes=5))
            return b.equity

        self.assertGreater(run(1.0), run(2.0))


if __name__ == "__main__":
    unittest.main()


class PartialCloseTest(unittest.TestCase):
    """close_position(symbol, qty) must close QTY, not everything.

    Found by audit, reconciling the first evaluation's exit mix: 2 target
    exits in 230, because _manage_open ignored the requested quantity and
    liquidated the whole position as "manual" one bar after every partial
    take. The engine banks 25%% and expects the rest to ride to the target or
    the drift-window exit; the sim sold it all. The strategy that was
    measured was not the strategy that was designed.
    """

    def setUp(self):
        self.cfg = ExecutionConfig()
        self.broker = SimBroker(100_000.0, self.cfg, cost_mult=1.0, ordering="worst")
        self.broker.submit(Order("NVDA", Side.BUY, 100, 90.0, 130.0, "qd-p1"))
        self.broker.on_bar("NVDA", bar("NVDA", NOW, 100.0, 100.5, 99.8, 100.2))

    def next_bar(self, offset_min=5, o=101.0, h=101.5, l=100.5, c=101.0):
        return bar("NVDA", NOW + timedelta(minutes=offset_min), o, h, l, c)

    def test_a_partial_close_leaves_the_rest_working(self):
        self.broker.close_position("NVDA", 25)
        events = self.broker.on_bar("NVDA", self.next_bar())
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].kind, "partial")
        self.assertEqual(events[0].quantity, 25)
        pos = self.broker._positions.get("NVDA")
        self.assertIsNotNone(pos, "the whole position was liquidated")
        self.assertEqual(pos.quantity, 75)

    def test_the_remainder_can_still_hit_the_target(self):
        self.broker.close_position("NVDA", 25)
        self.broker.on_bar("NVDA", self.next_bar())
        events = self.broker.on_bar("NVDA", self.next_bar(10, 129.0, 131.0, 128.5, 130.5))
        self.assertEqual(events[0].kind, "target")
        self.assertEqual(events[0].quantity, 75)

    def test_a_full_close_request_is_still_a_full_close(self):
        self.broker.close_position("NVDA")
        events = self.broker.on_bar("NVDA", self.next_bar())
        self.assertEqual(events[0].kind, "manual")
        self.assertEqual(events[0].quantity, 100)
        self.assertIsNone(self.broker._positions.get("NVDA"))

    def test_requesting_more_than_held_closes_what_is_held(self):
        self.broker.close_position("NVDA", 500)
        events = self.broker.on_bar("NVDA", self.next_bar())
        self.assertEqual(events[0].kind, "manual")
        self.assertEqual(events[0].quantity, 100)

    def test_partial_pnl_is_proportional(self):
        eq_before = self.broker.equity
        self.broker.close_position("NVDA", 50)
        ev = self.broker.on_bar("NVDA", self.next_bar())[0]
        # 50 shares closed ~1 point above entry: equity moves by roughly half
        # of what a full close would credit, never the full amount.
        gain = self.broker.equity - eq_before
        self.assertGreater(gain, 0)
        self.assertLess(gain, 50 * 1.6)


class JournalRollupTest(unittest.TestCase):
    """The journal must stay a record of decisions, not of their absence.

    135 symbols x thousands of cycles, almost all with no evidence at all,
    produced a 178 MB journal in twelve minutes and made the evaluation I/O
    bound on writing down that nothing happened. The counts still matter, so
    they are tallied rather than dropped.
    """

    class _Assessment:
        symbol = "TEST"
        trigger_score = 0.0
        conviction = 0.0
        direction = None
        agreeing_sources = 0
        opposing_weight = 0.0
        veto_score = 0.0
        per_source: dict = {}
        live_evidence: tuple = ()
        blocked = "no PEAD trigger"

    def journal(self):
        import tempfile, os
        from qd.journal import Journal
        fd, path = tempfile.mkstemp(suffix=".jsonl")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return Journal(path), path

    def test_empty_assessments_are_not_written_one_by_one(self):
        j, path = self.journal()
        for _ in range(50):
            j.assessment(self._Assessment(), taken=False)
        with open(path) as fh:
            self.assertEqual(fh.read().strip(), "", "empty assessments were written")

    def test_the_count_survives_in_the_rollup(self):
        j, path = self.journal()
        for _ in range(50):
            j.assessment(self._Assessment(), taken=False)
        j.flush_rollup()
        self.assertEqual(j.blocked_reasons().get("no PEAD trigger"), 50)

    def test_unflushed_counts_are_still_reported(self):
        """A run that ends mid-tally must not under-report its own gates."""
        j, _ = self.journal()
        for _ in range(7):
            j.assessment(self._Assessment(), taken=False)
        self.assertEqual(j.blocked_reasons().get("no PEAD trigger"), 7)

    def test_market_evidence_alone_is_still_absorbed(self):
        """Suppressing only the no-evidence case absorbs almost nothing —
        market structure produces evidence nearly every cycle, while the
        trigger stays silent."""
        from qd.types import Evidence, Source
        from datetime import datetime, timedelta, timezone
        a = self._Assessment()
        a.live_evidence = (Evidence(
            symbol="TEST", source=Source.MARKET, kind="trend", score=0.4,
            confidence=0.6, observed_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
            ttl=timedelta(hours=4),
        ),)
        j, path = self.journal()
        j.assessment(a, taken=False)
        with open(path) as fh:
            self.assertEqual(fh.read().strip(), "")

    def test_a_live_trigger_is_still_written_in_full(self):
        """Suppression must not reach anything that explains a decision."""
        from qd.types import Evidence, Source
        from datetime import datetime, timedelta, timezone
        a = self._Assessment()
        a.trigger_score = 0.62
        a.live_evidence = (Evidence(
            symbol="TEST", source=Source.EARNINGS, kind="pead", score=0.8,
            confidence=0.9, observed_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
            ttl=timedelta(days=3),
        ),)
        j, path = self.journal()
        j.assessment(a, taken=False, blocked="conviction below minimum")
        with open(path) as fh:
            body = fh.read()
        self.assertIn("conviction below minimum", body)
        self.assertIn("pead", body)
