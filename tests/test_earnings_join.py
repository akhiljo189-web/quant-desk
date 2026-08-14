"""
The join that dates every earnings event.

This is the highest-leverage place in the repository for a silent look-ahead
bug. XBRL knows WHAT a company earned and dates it by FISCAL QUARTER END; the
8-K knows WHEN the market found out. Use the quarter end as the event date and
the system starts trading a "drift" four to eight weeks before the
announcement — no error, plausible output, and a backtest that prints an
extraordinary equity curve for a strategy that cannot exist.

So the rule is absolute: a quarter that cannot be tied to a real filing is
DROPPED and counted. There is no "assume the usual date" path, and these tests
exist to keep one from being added.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.config import ProviderConfig
from qd.providers.earnings import (
    MAX_LAG, MIN_LAG, EarningsProvider, match_report, session_for,
)
from qd.providers.edgar import Filing
from qd.providers.xbrl import Quarter
from qd.types import UTC


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def filing(accepted: str, form: str = "8-K", items=("2.02",)) -> Filing:
    at = ts(accepted)
    return Filing(
        symbol="TEST", cik="0000000001", form=form,
        filed_date=at.replace(hour=0, minute=0, second=0),
        accepted_at=at, items=tuple(items), accession="0001-24-000001",
    )


def quarter(year: int, period: str, eps: float, end: str, filed: str) -> Quarter:
    e = ts(end)
    return Quarter(
        symbol="TEST", start=e - timedelta(days=91), end=e, eps=eps,
        filed=ts(filed), fiscal_year=year, fiscal_period=period,
    )


class SessionTest(unittest.TestCase):
    """bmo/amc/dmt comes from the real acceptance time, not a vendor flag —
    it drives the blackout window and the settling delay, both of which are
    wrong by hours if the marker is."""

    def test_before_the_open_is_bmo(self):
        self.assertEqual(session_for(ts("2024-05-02T11:03:00")), "bmo")   # 07:03 ET

    def test_after_the_close_is_amc(self):
        self.assertEqual(session_for(ts("2024-05-02T20:30:00")), "amc")   # 16:30 ET

    def test_during_the_session_is_dmt(self):
        self.assertEqual(session_for(ts("2024-05-02T18:00:00")), "dmt")   # 14:00 ET

    def test_the_boundary_belongs_to_the_session(self):
        self.assertEqual(session_for(ts("2024-05-02T13:30:00")), "dmt")   # 09:30 ET
        self.assertEqual(session_for(ts("2024-05-02T20:00:00")), "amc")   # 16:00 ET


class MatchReportTest(unittest.TestCase):
    def test_matches_the_release_that_followed_the_quarter(self):
        end = ts("2024-03-31T00:00:00")
        f = filing("2024-04-25T20:05:00")
        self.assertIs(match_report(end, [f]), f)

    def test_prefers_the_press_release_over_the_10q(self):
        """The market learned at the 8-K. The 10-Q can trail it by weeks, and
        dating the event there measures drift that already happened."""
        end = ts("2024-03-31T00:00:00")
        eight_k = filing("2024-04-25T20:05:00")
        ten_q = filing("2024-05-06T21:00:00", form="10-Q", items=())
        self.assertIs(match_report(end, [ten_q, eight_k]), eight_k)

    def test_falls_back_to_the_periodic_report(self):
        """Some filers report inside the 10-Q with no separate results 8-K.
        Less precise, but a real timestamp — used only when no 2.02 exists."""
        end = ts("2024-03-31T00:00:00")
        ten_q = filing("2024-05-06T21:00:00", form="10-Q", items=())
        self.assertIs(match_report(end, [ten_q]), ten_q)

    def test_a_filing_too_soon_is_not_the_report(self):
        """An 8-K two days after quarter end is something else — a departure,
        an acquisition. Results take weeks."""
        end = ts("2024-03-31T00:00:00")
        self.assertIsNone(match_report(end, [filing("2024-04-01T13:00:00")]))

    def test_a_filing_too_late_is_not_the_report(self):
        """Without the upper bound a missing filing silently matches the NEXT
        quarter's release, shifting the whole event by three months."""
        end = ts("2024-03-31T00:00:00")
        late = filing((ts("2024-03-31T00:00:00") + MAX_LAG + timedelta(days=2)).isoformat())
        self.assertIsNone(match_report(end, [late]))

    def test_no_filings_means_no_match_not_a_guess(self):
        self.assertIsNone(match_report(ts("2024-03-31T00:00:00"), []))


class StubFacts:
    def __init__(self, quarters):
        self._quarters = quarters

    def quarters(self, symbol, **kw):
        return list(self._quarters)

    def splits(self, symbol):
        return []


class StubEdgar:
    def __init__(self, filings):
        self._filings = filings

    def earnings_releases(self, symbol, start, end):
        return list(self._filings)


def clean_history() -> list[Quarter]:
    """Six years of ordinary quarters — enough history for SUE to be defined,
    with genuine dispersion so the scale is not degenerate."""
    eps = [1.00, 1.05, 0.95, 1.02,
           1.08, 1.00, 1.06, 0.98,
           1.06, 1.10, 0.99, 1.07,
           1.12, 1.08, 1.05, 1.10,
           1.15, 1.16, 1.06, 1.14,
           1.34, 1.19, 1.10, 1.17]      # a real beat in the final Q1: +3.4 SUE
    out = []
    for i, v in enumerate(eps):
        year, qi = 2019 + i // 4, i % 4 + 1
        end = datetime(year, 3, 31, tzinfo=UTC) + timedelta(days=91 * qi - 91)
        out.append(quarter(year, f"Q{qi}", v,
                           end.isoformat(), (end + timedelta(days=40)).isoformat()))
    return out


def releases_for(quarters) -> list[Filing]:
    """One item-2.02 8-K, 25 days after each quarter end.

    21:05 UTC, which is after the close in BOTH standard and daylight time —
    20:05 would be 15:05 ET in January and the session marker would flip
    halfway through the fixture.
    """
    return [filing((q.end + timedelta(days=25)).replace(hour=21, minute=5).isoformat())
            for q in quarters]


class SueJoinTest(unittest.TestCase):
    def setUp(self):
        self.cfg = ProviderConfig(sec_user_agent="test <test@example.com>")
        self.quarters = clean_history()

    def provider(self, quarters=None, filings=None) -> EarningsProvider:
        quarters = self.quarters if quarters is None else quarters
        filings = releases_for(quarters) if filings is None else filings
        p = EarningsProvider.__new__(EarningsProvider)
        p.cfg = self.cfg
        p.finnhub = None
        p.edgar = StubEdgar(filings)
        p._facts = StubFacts(quarters)
        p.last_stats = None
        return p

    def test_events_are_dated_by_the_filing_not_the_quarter_end(self):
        """THE test. A quarter ending 31 March announced on 25 April must be
        dated 25 April; dating it 31 March invents three weeks of drift that
        the strategy could not have traded."""
        p = self.provider()
        events = p.sue_earnings(["TEST"], ts("2019-01-01T00:00:00"),
                                ts("2026-01-01T00:00:00"), min_history=6)
        self.assertTrue(events)
        ends = {q.label: q.end for q in self.quarters}
        for e in events:
            self.assertIsNotNone(e.released_at)
            self.assertIn(e.fiscal_period, ends)
            # The announcement, not the period it covers.
            self.assertGreaterEqual(e.released_at - ends[e.fiscal_period], MIN_LAG)
            self.assertLessEqual(e.released_at - ends[e.fiscal_period], MAX_LAG)
            self.assertEqual(e.session, "amc")

    def test_unmatched_quarters_are_dropped_and_counted(self):
        """With no filings there is no honest date for anything, so nothing
        may come out — and the loss has to be visible as a number."""
        p = self.provider(filings=[])
        events = p.sue_earnings(["TEST"], ts("2019-01-01T00:00:00"),
                                ts("2026-01-01T00:00:00"), min_history=6)
        self.assertEqual(events, [])
        self.assertGreater(p.last_stats.rows, 0)
        self.assertEqual(p.last_stats.matched, 0)
        self.assertEqual(p.last_stats.rate, 0.0)

    def test_the_surprise_survives_the_round_trip(self):
        """`eps_surprise_pct()` must reproduce the SUE ratio, or the scoring
        and gating code silently means something different from what was
        tested."""
        p = self.provider()
        events = p.sue_earnings(["TEST"], ts("2019-01-01T00:00:00"),
                                ts("2026-01-01T00:00:00"), min_history=6)
        beat = [e for e in events if (e.eps_surprise_pct() or 0) > 0.1]
        self.assertTrue(beat, "the 1.40 quarter did not come through as a beat")
        e = beat[-1]
        self.assertAlmostEqual(
            e.eps_surprise_pct(),
            (e.eps_actual - e.eps_estimate) / abs(e.eps_estimate), places=9,
        )

    def test_actuals_are_not_knowable_before_the_release(self):
        p = self.provider()
        for e in p.sue_earnings(["TEST"], ts("2019-01-01T00:00:00"),
                                ts("2026-01-01T00:00:00"), min_history=6):
            self.assertFalse(e.has_actuals_at(e.released_at - timedelta(seconds=1)))
            self.assertTrue(e.has_actuals_at(e.released_at))

    def test_the_schedule_is_known_before_the_actuals(self):
        """`known_at` gates the blackout; `actuals_known_at` gates the signal.
        Collapsing the two hands the backtest the EPS in advance."""
        p = self.provider()
        for e in p.sue_earnings(["TEST"], ts("2019-01-01T00:00:00"),
                                ts("2026-01-01T00:00:00"), min_history=6):
            self.assertLess(e.known_at, e.actuals_known_at())

    def test_a_contaminated_rebound_never_becomes_an_event(self):
        """A writedown makes the following year read as an enormous beat. It
        must not reach the strategy at all — it arrives wearing maximum
        conviction, which is the worst way to be wrong."""
        qs = clean_history()
        # Replace one quarter with an impairment, and its successor a year
        # later with an ordinary result.
        qs[8] = quarter(qs[8].fiscal_year, "Q1", -20.0,
                        qs[8].end.isoformat(), qs[8].filed.isoformat())
        p = self.provider(quarters=qs, filings=releases_for(qs))
        events = p.sue_earnings(["TEST"], ts("2019-01-01T00:00:00"),
                                ts("2026-01-01T00:00:00"), min_history=6)
        self.assertGreater(p.last_stats.dropped_unreliable, 0)
        for e in events:
            self.assertLess(abs(e.eps_surprise_pct() or 0), 5.0)

    def test_keeping_unreliable_readings_is_an_explicit_choice(self):
        qs = clean_history()
        qs[8] = quarter(qs[8].fiscal_year, "Q1", -20.0,
                        qs[8].end.isoformat(), qs[8].filed.isoformat())
        p = self.provider(quarters=qs, filings=releases_for(qs))
        kept = p.sue_earnings(["TEST"], ts("2019-01-01T00:00:00"),
                              ts("2026-01-01T00:00:00"), min_history=6,
                              drop_unreliable=False)
        self.assertEqual(p.last_stats.dropped_unreliable, 0)
        p2 = self.provider(quarters=qs, filings=releases_for(qs))
        dropped = p2.sue_earnings(["TEST"], ts("2019-01-01T00:00:00"),
                                  ts("2026-01-01T00:00:00"), min_history=6)
        self.assertGreater(len(kept), len(dropped))

    def test_events_outside_the_window_are_not_returned(self):
        p = self.provider()
        events = p.sue_earnings(["TEST"], ts("2023-01-01T00:00:00"),
                                ts("2024-01-01T00:00:00"), min_history=6)
        for e in events:
            self.assertGreaterEqual(e.released_at, ts("2023-01-01T00:00:00"))
            self.assertLessEqual(e.released_at, ts("2024-01-01T00:00:00"))

    def test_a_thin_history_produces_nothing_rather_than_a_guess(self):
        p = self.provider(quarters=self.quarters[:6])
        self.assertEqual(
            p.sue_earnings(["TEST"], ts("2019-01-01T00:00:00"),
                           ts("2026-01-01T00:00:00"), min_history=6),
            [],
        )


if __name__ == "__main__":
    unittest.main()
