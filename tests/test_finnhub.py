"""
The Finnhub earnings adapter — parsing, and the leak it would otherwise cause.

Finnhub returns each event's CURRENT state, so a row for last quarter carries
the scheduled date and the actual EPS side by side with nothing distinguishing
when each became knowable. These tests pin the split that keeps the backtest
honest: the date is public weeks early, the numbers are not public until the
release instant.

All fixtures are literal response shapes, so the suite runs with no API key.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.config import EarningsConfig
from qd.features import earnings as ech
from qd.providers.finnhub import SCHEDULE_LEAD, parse_row
from qd.types import UTC

# A reported quarter: actuals present.
REPORTED = {
    "date": "2026-01-28",
    "epsActual": 2.18,
    "epsEstimate": 2.10,
    "hour": "amc",
    "quarter": 1,
    "revenueActual": 123945000000,
    "revenueEstimate": 121000000000,
    "symbol": "AAPL",
    "year": 2026,
}

# A scheduled-but-unreported quarter: estimates only, actuals null.
SCHEDULED = {
    "date": "2026-04-29",
    "epsActual": None,
    "epsEstimate": 1.55,
    "hour": "amc",
    "quarter": 2,
    "revenueActual": None,
    "revenueEstimate": 94000000000,
    "symbol": "AAPL",
    "year": 2026,
}

BEFORE_OPEN = {**REPORTED, "hour": "bmo", "symbol": "JPM"}
UNKNOWN_HOUR = {**REPORTED, "hour": "", "symbol": "XOM"}


class ParseTest(unittest.TestCase):
    def test_reported_row_parses(self):
        ev = parse_row(REPORTED)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.symbol, "AAPL")
        self.assertEqual(ev.session, "amc")
        self.assertAlmostEqual(ev.eps_actual, 2.18)
        self.assertAlmostEqual(ev.eps_estimate, 2.10)
        self.assertEqual(ev.fiscal_period, "Q1 2026")

    def test_report_date_keeps_its_calendar_day(self):
        """UTC midnight, not shifted into the previous evening by a timezone
        conversion — the bug that computed every blackout a day early."""
        ev = parse_row(REPORTED)
        self.assertEqual(ev.report_date.date().isoformat(), "2026-01-28")

    def test_surprise_is_computed_from_the_parsed_row(self):
        ev = parse_row(REPORTED)
        self.assertAlmostEqual(ev.eps_surprise_pct(), (2.18 - 2.10) / 2.10, places=6)

    def test_session_mapping(self):
        self.assertEqual(parse_row(BEFORE_OPEN).session, "bmo")
        # An unrecognised hour becomes a midday release — the worst case for a
        # blackout, which is the right default when the field is missing.
        self.assertEqual(parse_row(UNKNOWN_HOUR).session, "dmt")

    def test_malformed_rows_are_dropped_not_guessed(self):
        self.assertIsNone(parse_row({"symbol": "AAPL"}))              # no date
        self.assertIsNone(parse_row({"date": "2026-01-28"}))          # no symbol
        self.assertIsNone(parse_row({**REPORTED, "date": "not-a-date"}))

    def test_null_numerics_become_none_not_zero(self):
        """A missing estimate must not read as an estimate of 0.00 — that would
        manufacture an infinite surprise on the next beat."""
        ev = parse_row(SCHEDULED)
        self.assertIsNone(ev.eps_actual)
        self.assertIsNone(ev.revenue_actual)
        self.assertIsNotNone(ev.eps_estimate)


class KnownAtTest(unittest.TestCase):
    """The two-timestamp split, which is the whole point of this adapter."""

    def test_schedule_is_known_before_the_report(self):
        ev = parse_row(REPORTED)
        self.assertEqual(ev.known_at, ev.report_date - SCHEDULE_LEAD)
        self.assertLess(ev.known_at, ev.report_date)

    def test_actuals_are_not_known_when_the_schedule_is(self):
        """THE leak test. Finnhub hands us the date and the EPS in one row; if
        both were treated as knowable at the same moment, every backtest would
        read each quarter's results weeks early and PEAD would look perfect."""
        ev = parse_row(REPORTED)
        self.assertFalse(ev.has_actuals_at(ev.known_at))
        self.assertFalse(ev.has_actuals_at(ev.report_date))     # 00:00 on the day
        self.assertTrue(ev.has_actuals_at(ev.released_at))

    def test_actuals_appear_exactly_at_the_release_instant(self):
        ev = parse_row(REPORTED)
        self.assertFalse(ev.has_actuals_at(ev.released_at - timedelta(seconds=1)))
        self.assertTrue(ev.has_actuals_at(ev.released_at + timedelta(seconds=1)))

    def test_amc_release_is_after_the_close(self):
        ev = parse_row(REPORTED)
        # 16:15 ET on 2026-01-28 = 21:15 UTC (EST, UTC-5).
        self.assertEqual(ev.released_at.hour, 21)
        self.assertEqual(ev.released_at.minute, 15)

    def test_bmo_release_is_before_the_open(self):
        ev = parse_row(BEFORE_OPEN)
        self.assertEqual(ev.released_at.hour, 12)               # 07:00 ET = 12:00 UTC

    def test_unreported_event_has_no_release_time(self):
        """released_at must stay None until actuals exist — set eagerly, every
        future quarter becomes readable the moment its date is announced."""
        ev = parse_row(SCHEDULED)
        self.assertIsNone(ev.released_at)
        self.assertIsNone(ev.actuals_known_at())
        self.assertFalse(ev.has_actuals_at(ev.report_date + timedelta(days=365)))

    def test_live_arrival_time_overrides_the_assumption(self):
        seen = datetime(2026, 1, 20, 14, 30, tzinfo=UTC)
        ev = parse_row(REPORTED, known_at=seen)
        self.assertEqual(ev.known_at, seen)


class IntegrationTest(unittest.TestCase):
    """Parsed rows must drive the blackout and the PEAD trigger correctly."""

    def setUp(self):
        self.cfg = EarningsConfig()
        self.ev = parse_row(REPORTED)

    def test_blackout_fires_the_day_before(self):
        # 20 hours before an after-close release.
        now = self.ev.released_at - timedelta(hours=20)
        state = ech.blackout("AAPL", [self.ev], now, self.cfg)
        self.assertTrue(state.active, state.reason)

    def test_no_blackout_a_week_out(self):
        now = self.ev.released_at - timedelta(days=7)
        self.assertFalse(ech.blackout("AAPL", [self.ev], now, self.cfg).active)

    def test_no_pead_evidence_before_the_release(self):
        now = self.ev.released_at - timedelta(hours=1)
        self.assertEqual(ech.evaluate("AAPL", [self.ev], now, self.cfg), [])

    def test_pead_evidence_after_the_release(self):
        now = self.ev.released_at + timedelta(hours=2)
        got = ech.evaluate("AAPL", [self.ev], now, self.cfg)
        self.assertEqual(len(got), 1)
        self.assertGreater(got[0].score, 0)          # 3.8% beat
        self.assertEqual(got[0].observed_at, self.ev.released_at)

    def test_scheduled_event_causes_blackout_but_no_signal(self):
        """The asymmetry that makes the whole design work: the upcoming report
        can keep us out of a trade, while its (unknown) numbers cannot cause
        one."""
        ev = parse_row(SCHEDULED)
        now = datetime(2026, 4, 29, 12, 0, tzinfo=UTC)   # release day, pre-close
        self.assertTrue(ech.blackout("AAPL", [ev], now, self.cfg).active)
        self.assertEqual(ech.evaluate("AAPL", [ev], now, self.cfg), [])

    def test_a_miss_scores_negative(self):
        miss = parse_row({**REPORTED, "epsActual": 1.80})     # est 2.10
        now = miss.released_at + timedelta(hours=2)
        got = ech.evaluate("AAPL", [miss], now, self.cfg)
        self.assertEqual(len(got), 1)
        self.assertLess(got[0].score, 0)


if __name__ == "__main__":
    unittest.main()
