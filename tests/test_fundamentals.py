"""
Company profile, financials and management commentary.

The context the screener attaches once the insider signal has surfaced a name.
None of it is a signal — the design (§4) forbids narrative and estimates from
reaching any score — so these tests pin extraction correctness, not predictive
value.

Two traps are load-bearing and both were found against live EDGAR:

  `Revenues` is not the revenue tag for most modern filers. Intel reports
  `RevenueFromContractWithCustomerExcludingAssessedTax`; a reader keyed on the
  obvious concept reports no revenue at all, which reads as a company with no
  sales rather than as a bug.

  Funds file like companies. A screener over insider buying surfaces fund share
  classes whose filings are structurally identical to an operating company's —
  one claimed a $1.49bn "insider purchase" in the first real scan.

Runs with no network and no key.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from qd.providers.fundamentals import (
    CompanyProfile,
    Fundamentals,
    Metric,
    _extract_quotes,
    _pick_concept,
    _strip_html,
)
from qd.types import UTC


def facts(concept: str, rows: list[tuple[str, str, float, str]]) -> dict:
    """rows: (start, end, value, filed)"""
    return {concept: {"units": {"USD": [
        {"start": s, "end": e, "val": v, "filed": f, "form": "10-K", "fp": "FY"}
        for s, e, v, f in rows
    ]}}}


ANNUAL = [
    ("2023-01-01", "2023-12-31", 1000.0, "2024-02-15"),
    ("2024-01-01", "2024-12-31", 1200.0, "2025-02-15"),
    ("2025-01-01", "2025-12-31", 1560.0, "2026-02-15"),
]


class ProfileTest(unittest.TestCase):
    def test_an_operating_company_is_investable(self):
        p = CompanyProfile(cik="0000050863", name="INTEL CORP", tickers=("INTC",),
                           sic="3674", sic_description="Semiconductors")
        self.assertTrue(p.is_operating_company)
        self.assertFalse(p.is_fund)
        self.assertEqual(p.symbol, "INTC")

    def test_a_fund_is_excluded_by_sic_not_by_name(self):
        """The first real scan surfaced a fund share class claiming a $1.49bn
        insider purchase. The filter is the SEC's own classification."""
        for sic in ("6722", "6726", "6770", "6799"):
            p = CompanyProfile(cik="1", name="Some Capital Management",
                               tickers=("XXXX",), sic=sic)
            self.assertTrue(p.is_fund, sic)
            self.assertFalse(p.is_operating_company, sic)

    def test_a_real_company_with_trust_in_its_name_survives(self):
        """Name matching would fail in both directions — plenty of operating
        companies are called Trust or Partners, and plenty of funds are not."""
        p = CompanyProfile(cik="1", name="Northern Trust Corp", tickers=("NTRS",),
                           sic="6022")
        self.assertTrue(p.is_operating_company)

    def test_a_filer_with_no_ticker_is_not_investable(self):
        p = CompanyProfile(cik="1", name="Private Co", tickers=(), sic="3674")
        self.assertFalse(p.is_operating_company)


class ConceptChainTest(unittest.TestCase):
    """`Revenues` is the obvious tag and most modern filers do not use it."""

    def test_the_preferred_modern_concept_is_found(self):
        us = facts("RevenueFromContractWithCustomerExcludingAssessedTax", ANNUAL)
        got = _pick_concept(us, ("Revenues",
                                 "RevenueFromContractWithCustomerExcludingAssessedTax"), None)
        self.assertEqual(len(got), 3)
        self.assertEqual(got[0].concept,
                         "RevenueFromContractWithCustomerExcludingAssessedTax")

    def test_the_chain_falls_back_in_order(self):
        us = facts("Revenues", ANNUAL)
        got = _pick_concept(us, ("Revenues", "SalesRevenueNet"), None)
        self.assertEqual(got[0].concept, "Revenues")

    def test_an_absent_concept_yields_nothing_rather_than_zero(self):
        """Zero revenue and unknown revenue are different facts."""
        self.assertEqual(_pick_concept({}, ("Revenues",), None), ())

    def test_results_are_newest_first(self):
        us = facts("Revenues", ANNUAL)
        got = _pick_concept(us, ("Revenues",), None)
        self.assertEqual(got[0].period_end.year, 2025)
        self.assertEqual(got[-1].period_end.year, 2023)

    def test_quarterly_and_cumulative_rows_are_rejected(self):
        """A 10-K carries three-month and nine-month rows under the same tag.
        Taking one silently turns a quarter into a year."""
        us = facts("Revenues", [("2025-10-01", "2025-12-31", 400.0, "2026-02-15")])
        self.assertEqual(_pick_concept(us, ("Revenues",), None), ())

    def test_a_restatement_does_not_replace_the_original(self):
        """The corrected value was not available when the original was filed —
        the same rule providers/xbrl.py applies to fundamentals."""
        us = facts("Revenues", [
            ("2025-01-01", "2025-12-31", 1560.0, "2026-02-15"),
            ("2025-01-01", "2025-12-31", 1490.0, "2026-08-01"),   # restated later
        ])
        got = _pick_concept(us, ("Revenues",), None)
        self.assertEqual(len(got), 1)
        self.assertAlmostEqual(got[0].value, 1560.0)

    def test_point_in_time_cut_hides_later_filings(self):
        us = facts("Revenues", ANNUAL)
        got = _pick_concept(us, ("Revenues",), datetime(2025, 6, 1, tzinfo=UTC))
        self.assertEqual(len(got), 2)
        self.assertEqual(got[0].period_end.year, 2024)


class DerivedTest(unittest.TestCase):
    def _f(self, **kw) -> Fundamentals:
        rev = _pick_concept(facts("Revenues", ANNUAL), ("Revenues",), None)
        return Fundamentals(cik="1", revenue=rev, **kw)

    def test_revenue_growth(self):
        self.assertAlmostEqual(self._f().revenue_growth_yoy, 0.30)

    def test_growth_needs_two_years(self):
        one = _pick_concept(facts("Revenues", ANNUAL[-1:]), ("Revenues",), None)
        self.assertIsNone(Fundamentals(cik="1", revenue=one).revenue_growth_yoy)

    def test_acceleration_is_the_second_difference(self):
        """8% -> 14% -> 23% and 23% -> 14% -> 8% have the same growth LEVEL and
        opposite meanings. Only the second difference tells them apart."""
        f = self._f()
        self.assertIsNotNone(f.revenue_acceleration)
        self.assertGreater(f.revenue_acceleration, 0)   # 20% then 30%

    def test_deceleration_is_negative(self):
        rows = [("2023-01-01", "2023-12-31", 1000.0, "2024-02-15"),
                ("2024-01-01", "2024-12-31", 1300.0, "2025-02-15"),
                ("2025-01-01", "2025-12-31", 1430.0, "2026-02-15")]
        rev = _pick_concept(facts("Revenues", rows), ("Revenues",), None)
        self.assertLess(Fundamentals(cik="1", revenue=rev).revenue_acceleration, 0)

    def test_margin_requires_matching_periods(self):
        """A margin built from this year's profit and last year's revenue is
        not a margin."""
        gp = (Metric(600.0, "GrossProfit", datetime(2024, 12, 31, tzinfo=UTC),
                     datetime(2025, 2, 15, tzinfo=UTC)),)
        self.assertIsNone(self._f(gross_profit=gp).gross_margin)

    def test_margin_computes_when_periods_match(self):
        gp = (Metric(780.0, "GrossProfit", datetime(2025, 12, 31, tzinfo=UTC),
                     datetime(2026, 2, 15, tzinfo=UTC)),)
        self.assertAlmostEqual(self._f(gross_profit=gp).gross_margin, 0.5)

    def test_net_debt_can_be_negative(self):
        end = datetime(2025, 12, 31, tzinfo=UTC)
        filed = datetime(2026, 2, 15, tzinfo=UTC)
        f = self._f(debt=(Metric(100.0, "LongTermDebt", end, filed),),
                    cash=(Metric(400.0, "Cash", end, filed),))
        self.assertAlmostEqual(f.net_debt, -300.0)

    def test_missing_inputs_give_none_not_zero(self):
        f = Fundamentals(cik="1")
        self.assertIsNone(f.revenue_growth_yoy)
        self.assertIsNone(f.gross_margin)
        self.assertIsNone(f.net_debt)


class CommentaryTest(unittest.TestCase):
    def test_an_attributed_quote_is_extracted(self):
        text = _strip_html(
            '<p>&ldquo;We grew revenue 30% and expanded margins for the fourth '
            'consecutive quarter, driven by strong demand in our data centre '
            'segment,&rdquo; said Jane Doe, Chief Executive Officer.</p>')
        got = _extract_quotes(text, 3)
        self.assertEqual(len(got), 1)
        self.assertIn("Jane Doe", got[0])
        self.assertIn("grew revenue 30%", got[0])

    def test_attribution_before_the_quote_also_works(self):
        text = ('commented John Smith, "Demand has been extraordinary this year '
                'and we are investing ahead of it across every product line we '
                'operate in today."')
        self.assertEqual(len(_extract_quotes(text, 3)), 1)

    def test_unattributed_marketing_copy_is_ignored(self):
        """A sentence with no speaker is press-release filler. A quote with a
        name attached is a person on the record."""
        text = ('"We are excited about the tremendous opportunity ahead of us '
                'and remain confident in our long term strategy going forward."')
        self.assertEqual(_extract_quotes(text, 3), ())

    def test_the_quote_limit_is_respected(self):
        one = ('&ldquo;Quote number {n} about our results this quarter and the '
               'outlook for the coming year across the business,&rdquo; said '
               'Jane Doe, CEO.')
        text = _strip_html(" ".join(one.format(n=i) for i in range(6)))
        self.assertLessEqual(len(_extract_quotes(text, 2)), 2)

    def test_html_is_stripped_and_entities_decoded(self):
        self.assertEqual(_strip_html("<p>a &amp; b</p>"), "a & b")


if __name__ == "__main__":
    unittest.main()
