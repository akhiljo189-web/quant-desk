"""
The look-ahead guards.

The most important tests in the repository. Every other test checks that a
component computes what it claims; these check that the system cannot see the
future — the failure that produces beautiful, worthless results and announces
itself with no error at all.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from qd.clock import SimClock
from qd.features.market import BarSeries
from qd.providers.replay import LookAheadError, ReplayDataset, ReplayProvider
from qd.types import Bar, EarningsEvent, NewsItem, UTC

T0 = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)


def bar(sym: str, start: datetime, minutes: int = 5, close: float = 100.0) -> Bar:
    return Bar(
        symbol=sym, start=start, end=start + timedelta(minutes=minutes),
        open=close, high=close + 1, low=close - 1, close=close, volume=10_000,
    )


class BarKnownAtTest(unittest.TestCase):
    def test_bar_is_known_at_its_close_not_its_open(self):
        b = bar("AAPL", T0)
        self.assertEqual(b.known_at, T0 + timedelta(minutes=5))
        self.assertEqual(b.event_time, T0)

    def test_series_hides_bars_that_have_not_closed(self):
        s = BarSeries("AAPL", [bar("AAPL", T0 + timedelta(minutes=5 * i)) for i in range(5)])
        # At T0+7min exactly one bar (the 14:30-14:35) has closed.
        visible = s.visible_at(T0 + timedelta(minutes=7))
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0].start, T0)

    def test_bar_forming_right_now_is_never_visible(self):
        s = BarSeries("AAPL", [bar("AAPL", T0)])
        # One second before the close, the bar does not exist yet.
        self.assertEqual(len(s.visible_at(T0 + timedelta(minutes=5) - timedelta(seconds=1))), 0)
        self.assertEqual(len(s.visible_at(T0 + timedelta(minutes=5))), 1)


class ReplayProviderTest(unittest.TestCase):
    def setUp(self):
        self.ds = ReplayDataset()
        self.ds.add_bars("AAPL", [
            bar("AAPL", T0 + timedelta(minutes=5 * i), close=100 + i) for i in range(20)
        ])
        self.ds.add_news([
            NewsItem(
                id="n1", symbols=("AAPL",), headline="AAPL raises FY guidance",
                published_at=T0 + timedelta(minutes=30),
                received_at=T0 + timedelta(minutes=31),
                source="reuters",
            )
        ])
        self.ds.freeze()

    def test_strict_mode_raises_on_future_request(self):
        clock = SimClock(T0 + timedelta(minutes=10))
        p = ReplayProvider(self.ds, clock, strict=True)
        with self.assertRaises(LookAheadError):
            p.bars("AAPL", T0, T0 + timedelta(hours=2))

    def test_lenient_mode_clamps_to_now(self):
        clock = SimClock(T0 + timedelta(minutes=12))
        p = ReplayProvider(self.ds, clock, strict=False)
        bars = p.bars("AAPL", T0, T0 + timedelta(hours=2))
        self.assertTrue(all(b.known_at <= clock.now() for b in bars))
        self.assertEqual(len(bars), 2)

    def test_news_is_invisible_before_it_is_received(self):
        clock = SimClock(T0 + timedelta(minutes=30, seconds=30))
        p = ReplayProvider(self.ds, clock, strict=False)
        # Published 30 seconds ago, not received until minute 31.
        self.assertEqual(len(p.news(["AAPL"], T0, clock.now())), 0)
        clock.set(T0 + timedelta(minutes=31))
        self.assertEqual(len(p.news(["AAPL"], T0, clock.now())), 1)

    def test_advancing_the_clock_reveals_strictly_more(self):
        """Monotonicity: information is only ever added, never withdrawn."""
        clock = SimClock(T0)
        p = ReplayProvider(self.ds, clock, strict=False)
        seen = 0
        for i in range(1, 21):
            clock.set(T0 + timedelta(minutes=5 * i))
            n = len(p.bars("AAPL", T0, clock.now()))
            self.assertGreaterEqual(n, seen)
            seen = n

    def test_clock_cannot_move_backwards(self):
        clock = SimClock(T0)
        clock.set(T0 + timedelta(minutes=5))
        with self.assertRaises(ValueError):
            clock.set(T0)


class EarningsLeakTest(unittest.TestCase):
    """The subtle one: the schedule is public early, the numbers are not."""

    def setUp(self):
        self.release = T0 + timedelta(days=3)
        self.ev = EarningsEvent(
            symbol="AAPL",
            report_date=datetime(2026, 3, 13, tzinfo=UTC),
            session="amc",
            scheduled_known_at=T0 - timedelta(days=20),
            eps_estimate=2.00, eps_actual=2.40,
            released_at=self.release,
        )

    def test_schedule_known_early(self):
        self.assertLess(self.ev.known_at, T0)

    def test_actuals_hidden_until_release(self):
        self.assertFalse(self.ev.has_actuals_at(T0))
        self.assertFalse(self.ev.has_actuals_at(self.release - timedelta(seconds=1)))
        self.assertTrue(self.ev.has_actuals_at(self.release))

    def test_provider_serves_schedule_but_evaluate_hides_numbers(self):
        """The blackout needs tomorrow's report today; PEAD must not get the EPS."""
        from qd.config import EarningsConfig
        from qd.features import earnings as ech

        ds = ReplayDataset()
        ds.add_earnings([self.ev])
        ds.freeze()
        clock = SimClock(T0)
        p = ReplayProvider(ds, clock, strict=False)

        # Schedule visible (so the blackout can fire) ...
        found = p.earnings(["AAPL"], T0 - timedelta(days=30), T0 + timedelta(days=30))
        self.assertEqual(len(found), 1)

        # ... but no PEAD evidence, because the numbers are not out.
        self.assertEqual(ech.evaluate("AAPL", found, T0, EarningsConfig()), [])

        # After the release, the evidence appears.
        clock.set(self.release + timedelta(minutes=1))
        ev = ech.evaluate("AAPL", found, clock.now(), EarningsConfig())
        self.assertEqual(len(ev), 1)
        self.assertGreater(ev[0].score, 0)      # a 20% beat is positive


class EvidenceDecayTest(unittest.TestCase):
    def test_expired_evidence_scores_zero(self):
        from qd.types import Evidence, Source

        e = Evidence(
            source=Source.NEWS, kind="guidance_raise", symbol="AAPL",
            score=1.0, confidence=1.0, observed_at=T0, ttl=timedelta(hours=1),
        )
        self.assertEqual(e.decayed_score(T0), 1.0)
        self.assertAlmostEqual(e.decayed_score(T0 + timedelta(minutes=20)), 0.5, places=6)
        self.assertEqual(e.decayed_score(T0 + timedelta(hours=2)), 0.0)
        self.assertFalse(e.is_live(T0 + timedelta(hours=2)))


if __name__ == "__main__":
    unittest.main()
