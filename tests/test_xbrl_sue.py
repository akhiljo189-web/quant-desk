"""
XBRL parsing and time-series SUE.

Every test here corresponds to a bug found against real SEC data. Each one
produced entirely plausible numbers — which is why they need pinning rather
than trusting:

  the YTD trap        a nine-month figure read as a quarter (~200% fake surprise)
  the split trap      Apple's 4:1 turning a flat quarter into "-75%"
  the missing Q4      a quarter of the sample gone, always the same quarter
  the one-off trap    an impairment making the next year read as +147%
  the lookahead trap  scaling SUE by the full-sample stdev, including the future
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta

from qd.providers.xbrl import (
    MAX_QUARTER_DAYS, MIN_QUARTER_DAYS, Quarter, compute_sue, derive_q4,
    fiscal_label, fiscal_year_end_month, seasonal_differences,
    split_factor_between, sue_to_surprise_pct,
)
from qd.types import UTC


def q(year: int, period: str, eps: float, end: str, days: int = 91,
      filed: str = None, symbol: str = "TEST") -> Quarter:
    e = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=UTC)
    return Quarter(
        symbol=symbol, start=e - timedelta(days=days), end=e, eps=eps,
        filed=datetime.strptime(filed or end, "%Y-%m-%d").replace(tzinfo=UTC),
        fiscal_year=year, fiscal_period=period,
    )


# Three years of ordinary results: mild growth with genuine dispersion, giving
# seasonal differences of roughly ±0.05. Deliberately NOT a repeated block and
# NOT a smooth ramp — an exactly-repeating pattern has zero seasonal variance
# and a smooth one has near-zero, and in both cases `compute_sue` is right to
# treat a rounding-sized move as enormous. Fixtures built that way measure the
# degenerate case rather than the one under test.
CLEAN = [1.00, 1.05, 0.95, 1.02,
         1.05, 1.02, 1.03, 1.00,
         1.07, 1.08, 0.99, 1.05]


def ladder(values, start_year=2020) -> list[Quarter]:
    """A clean quarterly sequence: 4 quarters/year, ~91 days apart."""
    out, y, qi = [], start_year, 1
    base = datetime(start_year, 3, 31, tzinfo=UTC)
    for i, v in enumerate(values):
        end = base + timedelta(days=91 * i)
        out.append(q(y + (i // 4), f"Q{(i % 4) + 1}", v, end.strftime("%Y-%m-%d")))
    return out


class FiscalLabelTest(unittest.TestCase):
    """XBRL's `fy`/`fp` name the FILING, not the period the fact covers, so
    every label is derived from the period end instead. A 10-K restates the
    prior year's quarters and tags them all fp="FY" with March, June and
    September end dates — matching on that pairs a Q3 against a Q1."""

    def d(self, s: str) -> datetime:
        return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=UTC)

    def test_calendar_year_filer(self):
        self.assertEqual(fiscal_label(self.d("2024-03-31"), 12), (2024, "Q1"))
        self.assertEqual(fiscal_label(self.d("2024-06-30"), 12), (2024, "Q2"))
        self.assertEqual(fiscal_label(self.d("2024-09-30"), 12), (2024, "Q3"))
        self.assertEqual(fiscal_label(self.d("2024-12-31"), 12), (2024, "Q4"))

    def test_septembers_filer_puts_december_in_the_next_fiscal_year(self):
        """Apple's quarter ending December 2023 is FY2024 Q1. Calling it Q4
        2023 compares it against the wrong quarter forever."""
        self.assertEqual(fiscal_label(self.d("2023-12-30"), 9), (2024, "Q1"))
        self.assertEqual(fiscal_label(self.d("2024-03-30"), 9), (2024, "Q2"))
        self.assertEqual(fiscal_label(self.d("2024-06-29"), 9), (2024, "Q3"))
        self.assertEqual(fiscal_label(self.d("2024-09-28"), 9), (2024, "Q4"))

    def test_52_53_week_drift_stays_in_one_quarter(self):
        """A 'Sunday nearest 31 December' year end lands anywhere from 28
        December to 3 January. Reading the month off the raw date splits one
        fiscal quarter across two labels."""
        for end in ("2023-12-28", "2023-12-31", "2024-01-02", "2024-01-03"):
            self.assertEqual(fiscal_label(self.d(end), 12), (2023, "Q4"), end)

    def test_off_grid_periods_are_refused(self):
        """A period that does not sit on the company's quarterly grid gets no
        label — a wrong one is worse than none."""
        self.assertIsNone(fiscal_label(self.d("2024-05-31"), 12))

    def test_year_end_month_is_read_from_the_annual_periods(self):
        ends = [self.d(x) for x in ("2022-09-24", "2023-09-30", "2024-09-28")]
        self.assertEqual(fiscal_year_end_month(ends), 9)

    def test_year_end_month_survives_a_january_rollover(self):
        ends = [self.d(x) for x in ("2022-12-31", "2024-01-02", "2024-12-28")]
        self.assertEqual(fiscal_year_end_month(ends), 12)


class RowParsingTest(unittest.TestCase):
    """`_to_quarters` against the row shapes SEC actually returns."""

    def rows(self):
        """One fiscal year of an Apple-shaped filer: quarters from 10-Qs, then
        the SAME quarters restated inside the 10-K as fp='FY', plus the YTD
        cumulative figures that share their end dates."""
        return [
            # 10-Q three-month figures
            {"start": "2023-10-01", "end": "2023-12-30", "val": 2.18,
             "filed": "2024-02-02", "fy": 2024, "fp": "Q1", "form": "10-Q"},
            {"start": "2023-12-31", "end": "2024-03-30", "val": 1.53,
             "filed": "2024-05-03", "fy": 2024, "fp": "Q2", "form": "10-Q"},
            {"start": "2024-03-31", "end": "2024-06-29", "val": 1.40,
             "filed": "2024-08-02", "fy": 2024, "fp": "Q3", "form": "10-Q"},
            # the YTD trap: six months, same end date as Q2
            {"start": "2023-10-01", "end": "2024-03-30", "val": 3.71,
             "filed": "2024-05-03", "fy": 2024, "fp": "Q2", "form": "10-Q"},
            # the fp trap: the 10-K restates Q1 as fp="FY"
            {"start": "2023-10-01", "end": "2023-12-30", "val": 2.18,
             "filed": "2024-11-01", "fy": 2025, "fp": "FY", "form": "10-K"},
            # the annual, used to derive Q4
            {"start": "2023-10-01", "end": "2024-09-28", "val": 6.08,
             "filed": "2024-11-01", "fy": 2024, "fp": "FY", "form": "10-K"},
        ]

    def parse(self):
        from qd.providers.xbrl import XbrlFacts
        f = XbrlFacts.__new__(XbrlFacts)
        rows = self.rows()
        fy_end = f._fiscal_year_end(rows)
        return fy_end, f._to_quarters("AAPL", rows, "EPSDiluted", None, fy_end)

    def test_year_end_month_comes_from_the_annual_row(self):
        self.assertEqual(self.parse()[0], 9)

    def test_the_ytd_row_is_not_mistaken_for_a_quarter(self):
        """3.71 is six months. Kept as Q2 it is a 140% fake surprise."""
        self.assertNotIn(3.71, [q.eps for q in self.parse()[1]])

    def test_the_10k_restatement_does_not_become_a_second_q1(self):
        quarters = self.parse()[1]
        q1 = [q for q in quarters if q.label == "Q1 2024"]
        self.assertEqual(len(q1), 1)
        # and it keeps the ORIGINAL 10-Q filing date, not the 10-K's
        self.assertEqual(q1[0].filed.date().isoformat(), "2024-02-02")

    def test_labels_are_derived_not_copied_from_fp(self):
        """The 10-K row carried fy=2025 fp='FY' for a December quarter."""
        labels = sorted(q.label for q in self.parse()[1])
        self.assertEqual(labels, ["Q1 2024", "Q2 2024", "Q3 2024"])


class SeasonalMatchingTest(unittest.TestCase):
    def test_pairs_match_on_fiscal_label_not_position(self):
        """A missing quarter must not shift the comparison onto the wrong one —
        the bug that paired Apple's 2021-12 against 2020-09, 455 days apart."""
        qs = ladder([1.0, 2.0, 3.0, 4.0, 1.5, 2.5, 3.5, 4.5])
        del qs[5]                       # drop Q2 of the second year
        pairs = seasonal_differences(qs)
        for quarter, diff in pairs:
            self.assertIn(quarter.fiscal_period, ("Q1", "Q2", "Q3", "Q4"))
        # Q1 must compare against Q1, giving 1.5 - 1.0.
        q1 = [d for qt, d in pairs if qt.fiscal_period == "Q1"]
        self.assertTrue(q1)
        self.assertAlmostEqual(q1[0], 0.5, places=6)

    def test_no_pair_without_a_year_ago_quarter(self):
        self.assertEqual(seasonal_differences(ladder([1.0, 2.0, 3.0])), [])


class SplitAdjustmentTest(unittest.TestCase):
    def test_split_factor_divides_historical_eps(self):
        d = datetime(2020, 8, 31, tzinfo=UTC)
        earlier = datetime(2020, 6, 1, tzinfo=UTC)
        later = datetime(2020, 12, 1, tzinfo=UTC)
        self.assertAlmostEqual(split_factor_between(earlier, later, [(d, 4.0)]), 0.25)

    def test_split_outside_the_window_is_ignored(self):
        d = datetime(2019, 1, 1, tzinfo=UTC)
        self.assertEqual(
            split_factor_between(datetime(2020, 6, 1, tzinfo=UTC),
                                 datetime(2020, 12, 1, tzinfo=UTC), [(d, 4.0)]),
            1.0,
        )

    def test_flat_earnings_across_a_split_show_no_surprise(self):
        """THE regression test. Apple's 4:1 split made a flat quarter read as
        a 75% collapse — a maximum-conviction short on an accounting event."""
        qs = ladder([4.0, 4.0, 4.0, 4.0, 1.0, 1.0, 1.0, 1.0])   # 4:1 mid-series
        split_date = qs[4].end - timedelta(days=10)
        pairs = seasonal_differences(qs, [(split_date, 4.0)])
        self.assertTrue(pairs)
        for _, diff in pairs:
            self.assertAlmostEqual(diff, 0.0, places=6)

    def test_without_adjustment_the_split_fabricates_a_surprise(self):
        qs = ladder([4.0, 4.0, 4.0, 4.0, 1.0, 1.0, 1.0, 1.0])
        pairs = seasonal_differences(qs, [])        # no split data
        self.assertTrue(any(abs(d) > 2.0 for _, d in pairs))


class Q4DerivationTest(unittest.TestCase):
    def test_q4_is_annual_minus_the_first_three(self):
        quarters = [q(2024, "Q1", 1.0, "2024-03-31"),
                    q(2024, "Q2", 2.0, "2024-06-30"),
                    q(2024, "Q3", 3.0, "2024-09-30")]
        annual = Quarter(
            symbol="TEST", start=datetime(2023, 12, 31, tzinfo=UTC),
            end=datetime(2024, 12, 31, tzinfo=UTC), eps=10.0,
            filed=datetime(2025, 2, 15, tzinfo=UTC),
            fiscal_year=2024, fiscal_period="FY",
        )
        derived = derive_q4(quarters, [annual])
        self.assertEqual(len(derived), 1)
        self.assertAlmostEqual(derived[0].eps, 4.0)
        self.assertEqual(derived[0].fiscal_period, "Q4")

    def test_derived_q4_inherits_the_10k_filing_date(self):
        """Q4 becomes publicly derivable only when the 10-K lands — dating it
        earlier would let the backtest read it before it existed."""
        quarters = [q(2024, "Q1", 1.0, "2024-03-31"),
                    q(2024, "Q2", 2.0, "2024-06-30"),
                    q(2024, "Q3", 3.0, "2024-09-30")]
        filed = datetime(2025, 2, 15, tzinfo=UTC)
        annual = Quarter(symbol="TEST", start=datetime(2023, 12, 31, tzinfo=UTC),
                         end=datetime(2024, 12, 31, tzinfo=UTC), eps=10.0,
                         filed=filed, fiscal_year=2024, fiscal_period="FY")
        self.assertEqual(derive_q4(quarters, [annual])[0].filed, filed)

    def test_incomplete_year_is_not_derived(self):
        """Two quarters plus an annual cannot yield Q4 — guessing would invent
        an earnings figure."""
        quarters = [q(2024, "Q1", 1.0, "2024-03-31"), q(2024, "Q2", 2.0, "2024-06-30")]
        annual = Quarter(symbol="TEST", start=datetime(2023, 12, 31, tzinfo=UTC),
                         end=datetime(2024, 12, 31, tzinfo=UTC), eps=10.0,
                         filed=datetime(2025, 2, 15, tzinfo=UTC),
                         fiscal_year=2024, fiscal_period="FY")
        self.assertEqual(derive_q4(quarters, [annual]), [])


class SueTest(unittest.TestCase):
    def test_sue_scales_by_the_companys_own_volatility(self):
        """A 5c miss is trivial for a company that swings 40c and serious for
        one that never moves 3c — that is what *standardised* means.

        Both fixtures end with the SAME +0.2 seasonal move; only their history
        of past surprises differs, so any difference in SUE comes purely from
        the scaling.
        """
        # Small but non-zero jitter: a company with literally zero variance has
        # an undefined scale and is correctly skipped.
        steady = ladder([1.00, 1.01, 0.99, 1.00] * 3 + [1.01, 1.00, 1.00, 1.00]
                        + [1.21, 1.00, 1.00, 1.00])
        wild = ladder([1.0, 2.0, 0.5, 1.5] * 3 + [1.0, 2.0, 0.5, 1.5]
                      + [1.2, 2.0, 0.5, 1.5])
        s_steady = compute_sue(steady, min_history=4)
        s_wild = compute_sue(wild, min_history=4)
        self.assertTrue(s_steady, "steady company produced no SUE")
        self.assertTrue(s_wild, "volatile company produced no SUE")
        self.assertGreater(max(abs(u.sue) for u in s_steady),
                           max(abs(u.sue) for u in s_wild))

    def test_scale_uses_only_prior_quarters(self):
        """Scaling by the full-sample stdev leaks the future into the
        denominator, shrinking SUE precisely where things later turned
        volatile."""
        qs = ladder(CLEAN + [1.1, 9.0, 1.0, 1.0])
        sues = compute_sue(qs, min_history=4)
        early = [u for u in sues if u.quarter.end < qs[13].end]
        self.assertTrue(early)
        # The later 9.0 shock must not have inflated the early scale.
        self.assertLess(max(u.scale for u in early), 1.0)

    def test_winsorizing_never_binds_on_a_lone_shock(self):
        """The cap is NOT the defence against a one-off item, and believing it
        was is the bug this pins.

        A single outlier inflates the very denominator used to judge it. With n
        prior differences a lone shock of ANY magnitude scores exactly
        n/sqrt(n-1) — 3.6 at n=12 — so it slips under a ±4 cap however violent
        the writedown was. Multiplying the shock by 100 does not move it.
        """
        def rebound_sue(shock: float):
            qs = ladder(CLEAN + [shock, 1.0, 1.0, 1.0] + [1.0, 1.0, 1.0, 1.0])
            sues = compute_sue(qs, min_history=4, winsorize=4.0)
            return max(sues, key=lambda u: u.raw_sue)

        small, huge = rebound_sue(-20.0), rebound_sue(-2000.0)
        self.assertAlmostEqual(small.raw_sue, huge.raw_sue, delta=0.05)
        self.assertLess(huge.raw_sue, 4.0)
        self.assertFalse(huge.winsorized)

    def test_a_rebound_off_a_one_off_is_flagged_contaminated(self):
        """CROX booked -8.82 EPS on a writedown, making the next year read as
        +147%. The reading is caught by its SHAPE — an extreme difference of
        the opposite sign exactly one year earlier — not by its size."""
        qs = ladder(CLEAN + [-20.0, 1.0, 1.0, 1.0] + [1.0, 1.0, 1.0, 1.0])
        sues = compute_sue(qs, min_history=4)
        bad = [u for u in sues if u.contaminated]
        self.assertTrue(bad, "the writedown rebound was not flagged")
        self.assertEqual(bad[0].quarter.fiscal_period, "Q1")
        self.assertGreater(bad[0].sue, 3.0)      # would have traded at conviction
        self.assertFalse(bad[0].reliable)

    def test_an_ordinary_surprise_is_not_flagged_contaminated(self):
        """The flag must not fire on a normal run of results, or it silently
        deletes the strategy."""
        qs = ladder(CLEAN + [1.17, 1.12, 1.05, 1.11])
        sues = compute_sue(qs, min_history=4)
        self.assertTrue(sues)
        self.assertFalse(any(u.contaminated for u in sues))
        self.assertTrue(all(u.reliable for u in sues))

    def test_winsorized_flag_preserves_the_raw_value(self):
        """Broad volatility — not a lone shock — is what the cap actually
        catches, and the uncapped value stays readable."""
        # Diffs of ±0.2–0.4 every quarter, then one +2.00. The scale is set by
        # the broad spread rather than by the final move, so the ratio can
        # actually exceed 4.
        qs = ladder([1.00, 1.00, 1.00, 1.00,
                     1.30, 0.80, 1.40, 0.90,
                     1.50, 1.10, 1.10, 1.20,
                     1.70, 0.90, 1.40, 1.00,
                     3.70, 1.10, 1.60, 1.20])
        capped = [u for u in compute_sue(qs, min_history=4) if u.winsorized]
        self.assertTrue(capped, "no reading hit the cap")
        self.assertGreater(abs(capped[0].raw_sue), abs(capped[0].sue))
        self.assertLessEqual(abs(capped[0].sue), 4.0 + 1e-9)

    def test_scale_excludes_quarters_filed_later(self):
        """A quarter that ENDED earlier can be PUBLISHED later — a derived Q4
        carries its 10-K's date. Ordering the history by period end puts an
        unpublished figure in the denominator."""
        qs = ladder(CLEAN + [1.17, 1.12, 1.05, 1.11])
        # Backdate nothing; instead push one earlier quarter's filing past the
        # quarter being scored, as a 10-K derivation does.
        late = [replace(x, filed=x.filed + timedelta(days=400)) if i == 8 else x
                for i, x in enumerate(qs)]
        base = {u.label: u for u in compute_sue(qs, min_history=4)}
        after = {u.label: u for u in compute_sue(late, min_history=4)}
        moved = [k for k in base if k in after and
                 abs(base[k].scale - after[k].scale) > 1e-12]
        self.assertTrue(moved, "the late filing was still counted as prior")

    def test_zero_variance_company_produces_no_sue(self):
        """Dividing by a zero scale would be infinite conviction."""
        self.assertEqual(compute_sue(ladder([1.0] * 16), min_history=4), [])

    def test_min_history_is_respected(self):
        qs = ladder([1.0, 2.0, 3.0, 4.0] * 3)
        self.assertEqual(compute_sue(qs, min_history=99), [])

    def test_surprise_pct_matches_the_consensus_contract(self):
        """sue_to_surprise_pct must reproduce the ratio EarningsEvent expects,
        so the scoring and gating code needs no change for this switch."""
        qs = ladder([1.0, 1.0, 1.0, 1.0, 2.0, 1.0, 1.0, 1.0] * 2)
        sues = compute_sue(qs, min_history=4)
        for u in sues:
            pct = sue_to_surprise_pct(u)
            if pct is not None:
                self.assertAlmostEqual(pct, u.surprise / abs(u.expected), places=6)

    def test_near_zero_expectation_returns_none(self):
        """A 1c move against a 0.1c base is a rounding artefact, not a 900%
        surprise."""
        qs = ladder([0.001] * 8 + [0.01] * 8)
        for u in compute_sue(qs, min_history=4):
            if abs(u.expected) < 0.01:
                self.assertIsNone(sue_to_surprise_pct(u))


if __name__ == "__main__":
    unittest.main()
