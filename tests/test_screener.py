"""
The screener — assembling a candidate from filings.

This is the finder, not the study. The tests that matter most here are the ones
keeping the two apart, and the ones keeping "could not read it" distinct from
"there is nothing there".

Runs with no network and no key.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.features.insider import ScoreWeights
from qd.providers.forms import InsiderTransaction
from qd.providers.fundamentals import CompanyProfile, Fundamentals
from qd.providers.holdings13f import Holding, ShareType
from qd.types import UTC
from research.screener import (
    Candidate,
    build_candidate,
    rank,
    summarise_institutions,
)

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def txn(owner="0001", name="DOE JANE", shares=10_000, days_ago=10,
        accession="a1", title="Chief Executive Officer") -> InsiderTransaction:
    traded = NOW - timedelta(days=days_ago)
    return InsiderTransaction(
        symbol="EXMP", issuer_cik="0000123456", owner_cik=owner, owner_name=name,
        transaction_date=traded, accepted_at=traded + timedelta(days=2),
        code="P", acquired=True, shares=shares, price=20.0,
        shares_owned_after=110_000, is_officer=True, officer_title=title,
        accession=accession,
    )


def holding(cusip="123456789", value=1_000_000.0, shares=10_000,
            accession="f1", share_type=ShareType.SH) -> Holding:
    return Holding(
        cusip=cusip, issuer_name="Example Inc", title_of_class="COM",
        value_raw=value, value_usd=value, shares=shares, share_type=share_type,
        accepted_at=NOW - timedelta(days=20),
        period_of_report=NOW - timedelta(days=63), accession=accession,
    )


class BuildTest(unittest.TestCase):
    def test_a_candidate_is_built_from_transactions_alone(self):
        c = build_candidate([txn()], as_of=NOW)
        self.assertEqual(c.symbol, "EXMP")
        self.assertEqual(c.cik, "0000123456")
        self.assertGreater(c.insider.score, 0)
        self.assertTrue(c.included)

    def test_no_network_means_context_is_missing_not_wrong(self):
        c = build_candidate([txn()], as_of=NOW)
        self.assertIn("financials", c.missing_context)
        self.assertIn("management commentary", c.missing_context)

    def test_the_summary_line_renders(self):
        self.assertIn("EXMP", build_candidate([txn()], as_of=NOW).summary())


class WeightingSeparationTest(unittest.TestCase):
    """The screener ranks for a human; the study measures. Conflating them is
    how a ranking preference becomes a research finding."""

    def test_the_screener_uses_its_own_weighting(self):
        c = build_candidate([txn()], as_of=NOW)
        self.assertEqual(c.insider.weighting, "screener")

    def test_the_registered_weighting_is_unchanged_and_still_the_default(self):
        from qd.features.insider import score_insiders
        s = score_insiders([txn()], as_of=NOW)
        self.assertEqual(s.weighting, "registered")

    def test_agreement_outranks_size_under_the_screener_weighting(self):
        """The defect found in the first real scan: a single large director buy
        outranked two officers buying together. Under the screener preset,
        agreement leads."""
        one_big = [txn(owner="0001", name="A", shares=40_000, accession="a1")]
        three = [txn(owner="0001", name="A", accession="a1"),
                 txn(owner="0002", name="B", accession="a2"),
                 txn(owner="0003", name="C", accession="a3")]
        self.assertGreater(build_candidate(three, as_of=NOW).insider.score,
                           build_candidate(one_big, as_of=NOW).insider.score)

    def test_the_registered_weighting_still_prefers_size(self):
        """Pinned so the presets cannot silently converge — if they did, the
        separation between finder and study would be cosmetic."""
        from qd.features.insider import score_insiders
        one_big = [txn(owner="0001", name="A", shares=40_000, accession="a1")]
        three = [txn(owner="0001", name="A", accession="a1"),
                 txn(owner="0002", name="B", accession="a2"),
                 txn(owner="0003", name="C", accession="a3")]
        self.assertGreater(score_insiders(one_big, as_of=NOW).score,
                           score_insiders(three, as_of=NOW).score)


class FundExclusionTest(unittest.TestCase):
    """A fund share class claiming a $1.49bn insider purchase topped the first
    real scan. Funds are excluded on the SEC's own classification."""

    class _Co:
        def __init__(self, profile): self._p = profile
        def profile(self, cik): return self._p
        def fundamentals(self, cik, as_of=None): return Fundamentals(cik=cik)
        def commentary(self, cik): return None

    def test_a_fund_is_excluded_with_a_stated_reason(self):
        co = self._Co(CompanyProfile(cik="0000123456", name="Bluearc Core Fund",
                                     tickers=("PBLSX",), sic="6726"))
        c = build_candidate([txn()], as_of=NOW, company=co)
        self.assertFalse(c.included)
        self.assertIn("investment vehicle", c.excluded_reason)
        self.assertIn("6726", c.excluded_reason)

    def test_a_filer_with_no_ticker_says_so_rather_than_quoting_an_empty_sic(self):
        co = self._Co(CompanyProfile(cik="0000123456", name="Some Filer",
                                     tickers=(), sic=""))
        c = build_candidate([txn()], as_of=NOW, company=co)
        self.assertFalse(c.included)
        self.assertEqual(c.excluded_reason, "no listed ticker")

    def test_an_operating_company_is_kept(self):
        co = self._Co(CompanyProfile(cik="0000123456", name="Example Inc",
                                     tickers=("EXMP",), sic="3674"))
        self.assertTrue(build_candidate([txn()], as_of=NOW, company=co).included)

    def test_exclusion_can_be_switched_off_deliberately(self):
        co = self._Co(CompanyProfile(cik="0000123456", name="A Fund",
                                     tickers=("XXXX",), sic="6726"))
        c = build_candidate([txn()], as_of=NOW, company=co,
                            require_operating_company=False)
        self.assertTrue(c.included)


class InstitutionsTest(unittest.TestCase):
    def test_none_means_not_looked_up(self):
        self.assertIsNone(summarise_institutions(None))

    def test_an_empty_result_is_available_false_not_a_crash(self):
        i = summarise_institutions([])
        self.assertIsNotNone(i)
        self.assertFalse(i.available)

    def test_holders_are_counted_by_filing(self):
        i = summarise_institutions([holding(accession="f1"),
                                    holding(accession="f2")])
        self.assertEqual(i.holders, 2)

    def test_bond_principal_is_not_counted_as_a_holding(self):
        """PRN is a face value in dollars sharing a field with share counts."""
        i = summarise_institutions([holding(accession="f1", share_type=ShareType.PRN)])
        self.assertFalse(i.available)

    def test_new_positions_need_a_prior_quarter(self):
        cur = [holding(accession="f1"), holding(accession="f2")]
        self.assertEqual(summarise_institutions(cur, [holding(accession="f1")]).new_positions, 1)

    def test_with_no_prior_quarter_nothing_is_new(self):
        """Otherwise every fund looks like a fresh buyer in the first quarter
        of any archive."""
        self.assertEqual(summarise_institutions([holding()]).new_positions, 0)

    def test_the_lag_is_stated_on_every_result(self):
        self.assertIn("45 days", summarise_institutions([holding()]).lag_note)


class RankTest(unittest.TestCase):
    def _c(self, score, included=True):
        c = build_candidate([txn(shares=int(score * 400))], as_of=NOW)
        return c if included else Candidate(
            symbol=c.symbol, cik=c.cik, name="", insider=c.insider,
            excluded_reason="not an operating company")

    def test_ranked_highest_first(self):
        out = rank([self._c(10), self._c(90), self._c(50)])
        self.assertEqual(out[0].insider.score, max(c.insider.score for c in out))

    def test_excluded_candidates_are_dropped_not_demoted(self):
        """'This is a fund' is not a weak signal — it is not a candidate."""
        self.assertEqual(len(rank([self._c(90, included=False), self._c(10)])), 1)

    def test_the_limit_is_respected(self):
        self.assertEqual(len(rank([self._c(i) for i in range(1, 40)], limit=5)), 5)


if __name__ == "__main__":
    unittest.main()
