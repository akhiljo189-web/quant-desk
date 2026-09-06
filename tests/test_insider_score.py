"""
The insider accumulation score — aggregation, and what must not inflate it.

The parser (`tests/test_form4.py`) decides what counts as a purchase. This
decides what a set of purchases is worth, and the failure modes are different:
here the danger is that one large filer, one repeated amendment, or one family
trust filing jointly reads as a crowd of independent insiders agreeing.

Point-in-time is enforced at this layer too, not only in the replay provider —
`known_at` filtering lives in the scorer because the scorer is what a research
loop calls directly.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.features.insider import WINDOW_DAYS, InsiderScore, score_insiders
from qd.providers.forms import InsiderTransaction
from qd.types import UTC

AS_OF = datetime(2026, 3, 1, tzinfo=UTC)


def txn(
    *,
    code: str = "P",
    owner: str = "0001111111",
    name: str = "DOE JANE",
    days_ago: int = 10,
    shares: float = 10_000,
    price: float = 20.0,
    owned_after: float = 110_000,
    officer: bool = True,
    director: bool = False,
    ten_pct: bool = False,
    title: str = "Chief Executive Officer",
    accession: str = "acc-1",
    amendment: bool = False,
    lag_days: int = 2,
) -> InsiderTransaction:
    traded = AS_OF - timedelta(days=days_ago)
    return InsiderTransaction(
        symbol="EXMP",
        issuer_cik="0000320193",
        owner_cik=owner,
        owner_name=name,
        transaction_date=traded,
        accepted_at=traded + timedelta(days=lag_days),
        code=code,
        acquired=(code == "P"),
        shares=shares,
        price=price,
        shares_owned_after=owned_after,
        is_officer=officer,
        is_director=director,
        is_ten_percent_owner=ten_pct,
        officer_title=title,
        accession=accession,
        is_amendment=amendment,
    )


class EmptyTest(unittest.TestCase):
    def test_no_transactions_scores_zero_not_none(self):
        s = score_insiders([], as_of=AS_OF)
        self.assertIsInstance(s, InsiderScore)
        self.assertEqual(s.score, 0.0)
        self.assertEqual(s.purchase_count, 0)

    def test_an_empty_score_is_not_a_negative_one(self):
        """No filings means no information, not bad news. A universe is mostly
        companies where nobody traded this quarter, and if silence scored
        negative the whole cross-section would be ranked by filing frequency."""
        self.assertEqual(score_insiders([], as_of=AS_OF).score, 0.0)


class PointInTimeTest(unittest.TestCase):
    def test_a_filing_not_yet_accepted_is_invisible(self):
        """The trade happened; the disclosure had not been published. This is
        the two-business-day gap the whole effect lives in."""
        t = txn(days_ago=1, lag_days=2)          # traded yesterday, filed tomorrow
        self.assertGreater(t.known_at, AS_OF)
        self.assertEqual(score_insiders([t], as_of=AS_OF).purchase_count, 0)

    def test_the_same_filing_counts_once_accepted(self):
        t = txn(days_ago=5, lag_days=2)
        self.assertLess(t.known_at, AS_OF)
        self.assertEqual(score_insiders([t], as_of=AS_OF).purchase_count, 1)

    def test_the_window_is_measured_on_known_at_not_the_trade_date(self):
        """A trade just outside the window whose filing landed inside it is
        actionable inside it. Windowing on the trade date would drop signals
        the system genuinely could have acted on."""
        t = txn(days_ago=WINDOW_DAYS + 1, lag_days=5)
        self.assertEqual(score_insiders([t], as_of=AS_OF).purchase_count, 1)

    def test_an_old_filing_falls_out_of_the_window(self):
        t = txn(days_ago=WINDOW_DAYS + 60, lag_days=2)
        self.assertEqual(score_insiders([t], as_of=AS_OF).purchase_count, 0)


class PurchaseIntensityTest(unittest.TestCase):
    def test_buying_more_relative_to_an_existing_stake_scores_higher(self):
        """Intensity is relative to what the insider already held, so it is not
        a proxy for personal wealth. A CEO adding 10% to their stake is the
        same signal whether the stake is $1M or $100M."""
        small = txn(shares=1_000, owned_after=101_000)     # +1% of holding
        large = txn(shares=50_000, owned_after=150_000)    # +50% of holding
        self.assertGreater(
            score_insiders([large], as_of=AS_OF).score,
            score_insiders([small], as_of=AS_OF).score,
        )

    def test_a_first_ever_purchase_does_not_divide_by_zero(self):
        """Someone with no prior stake buying for the first time is the
        strongest version of this signal, and it is the row that breaks a naive
        ratio."""
        s = score_insiders([txn(shares=10_000, owned_after=10_000)], as_of=AS_OF)
        self.assertGreater(s.score, 0.0)
        self.assertTrue(all(v == v for v in (s.score, s.intensity)))  # not NaN

    def test_a_missing_post_transaction_holding_still_scores(self):
        t = txn(owned_after=None)
        self.assertGreater(score_insiders([t], as_of=AS_OF).score, 0.0)


class ClusterTest(unittest.TestCase):
    def test_several_insiders_buying_beats_one_buying_at_the_same_intensity(self):
        """The design's core claim, and one of its registered falsification
        tests: independent agreement is the signal, not gross dollars.

        Per-buyer intensity is held constant — each adds 10,000 shares to a
        100,000 share stake — so the only variable is how many people did it.
        """
        one = [txn(owner="0001", name="A", accession="a1")]
        three = [
            txn(owner="0001", name="A", accession="a1"),
            txn(owner="0002", name="B", accession="a2"),
            txn(owner="0003", name="C", accession="a3"),
        ]
        self.assertGreater(
            score_insiders(three, as_of=AS_OF).score,
            score_insiders(one, as_of=AS_OF).score,
        )

    def test_one_concentrated_buy_can_outscore_a_weak_cluster(self):
        """A consequence of the design's registered weights (intensity 35,
        cluster 20), not an accident — pinned so that changing the weights has
        to be a deliberate act rather than a silent drift.

        Whether this ordering is CORRECT is an empirical question the decile
        test answers. The score must not be rigged to guarantee the cluster
        result it is supposed to be testing for.
        """
        concentrated = [txn(owner="0001", name="A", shares=30_000, accession="a1")]
        weak_cluster = [
            txn(owner="0001", name="A", shares=2_000, accession="a1"),
            txn(owner="0002", name="B", shares=2_000, accession="a2"),
            txn(owner="0003", name="C", shares=2_000, accession="a3"),
        ]
        self.assertGreater(
            score_insiders(concentrated, as_of=AS_OF).score,
            score_insiders(weak_cluster, as_of=AS_OF).score,
        )

    def test_cluster_counts_distinct_people(self):
        two = [
            txn(owner="0001", name="A", accession="a1"),
            txn(owner="0002", name="B", accession="a2"),
        ]
        self.assertEqual(score_insiders(two, as_of=AS_OF).cluster_size, 2)

    def test_one_person_filing_twice_is_not_a_cluster(self):
        repeat = [
            txn(owner="0001", name="A", accession="a1", days_ago=20),
            txn(owner="0001", name="A", accession="a2", days_ago=10),
        ]
        self.assertEqual(score_insiders(repeat, as_of=AS_OF).cluster_size, 1)

    def test_a_joint_filing_is_one_decision_not_a_cluster(self):
        """Two names on one accession is a family trust or a spousal holding —
        one decision reported twice. Counting it as two agreeing insiders is
        how a cluster score inflates itself."""
        joint = [
            txn(owner="0001", name="DOE JANE", accession="same"),
            txn(owner="0002", name="DOE JOHN", accession="same"),
        ]
        self.assertEqual(score_insiders(joint, as_of=AS_OF).cluster_size, 1)

    def test_a_ten_percent_owner_does_not_form_a_cluster(self):
        """A sponsor or fund crossing a threshold trades for portfolio reasons.
        It may score, but it is not an insider agreeing."""
        rows = [
            txn(owner="0001", name="A", accession="a1"),
            txn(owner="0009", name="BIGFUND LP", accession="a2",
                officer=False, director=False, ten_pct=True, title=""),
        ]
        self.assertEqual(score_insiders(rows, as_of=AS_OF).cluster_size, 1)


class SeniorityTest(unittest.TestCase):
    def test_a_ceo_buy_outscores_a_director_buy(self):
        ceo = txn(title="Chief Executive Officer", officer=True)
        director = txn(title="", officer=False, director=True)
        self.assertGreater(
            score_insiders([ceo], as_of=AS_OF).score,
            score_insiders([director], as_of=AS_OF).score,
        )

    def test_a_director_buy_outscores_a_ten_percent_holder_buy(self):
        director = txn(title="", officer=False, director=True)
        holder = txn(title="", officer=False, director=False, ten_pct=True)
        self.assertGreater(
            score_insiders([director], as_of=AS_OF).score,
            score_insiders([holder], as_of=AS_OF).score,
        )


class SellingTest(unittest.TestCase):
    def test_selling_reduces_the_score(self):
        buy = [txn(code="P")]
        buy_and_sell = buy + [
            txn(code="S", owner="0002", name="B", accession="a2", shares=40_000)
        ]
        self.assertLess(
            score_insiders(buy_and_sell, as_of=AS_OF).score,
            score_insiders(buy, as_of=AS_OF).score,
        )

    def test_selling_alone_does_not_push_the_score_below_zero(self):
        """The score is an accumulation measure on 0-100. Distribution is
        reported in its own field rather than as a negative accumulation, so a
        cross-sectional percentile rank stays meaningful."""
        s = score_insiders([txn(code="S", shares=50_000)], as_of=AS_OF)
        self.assertGreaterEqual(s.score, 0.0)
        self.assertGreater(s.sale_value, 0.0)

    def test_planned_sales_do_not_count_against_the_score(self):
        planned = InsiderTransaction(
            symbol="EXMP", issuer_cik="0000320193", owner_cik="0002",
            owner_name="B", transaction_date=AS_OF - timedelta(days=10),
            accepted_at=AS_OF - timedelta(days=8), code="S", acquired=False,
            shares=50_000, price=20.0, is_officer=True, planned_10b5_1=True,
            accession="a2",
        )
        buy = [txn(code="P")]
        self.assertAlmostEqual(
            score_insiders(buy + [planned], as_of=AS_OF).score,
            score_insiders(buy, as_of=AS_OF).score,
        )


class AmendmentTest(unittest.TestCase):
    def test_an_amendment_does_not_double_count_its_original(self):
        """A 4/A restates a filing already counted. Keeping both reports one
        purchase as two — and amendments cluster on large, complicated filers,
        so the inflation is not random across the cross-section."""
        original = txn(accession="a1", days_ago=20)
        amended = txn(accession="a1-a", days_ago=20, amendment=True)
        self.assertEqual(
            score_insiders([original, amended], as_of=AS_OF).purchase_count, 1
        )


class BoundsTest(unittest.TestCase):
    def test_the_score_is_bounded_at_100(self):
        rows = [
            txn(owner=f"{i:04d}", name=f"P{i}", accession=f"a{i}",
                shares=1_000_000, owned_after=1_000_000)
            for i in range(20)
        ]
        self.assertLessEqual(score_insiders(rows, as_of=AS_OF).score, 100.0)

    def test_the_score_is_bounded_at_zero(self):
        rows = [txn(code="S", shares=10_000_000) for _ in range(20)]
        self.assertGreaterEqual(score_insiders(rows, as_of=AS_OF).score, 0.0)

    def test_the_score_reports_which_components_are_missing(self):
        """13D/G and 13F are not built yet. A caller must be able to tell this
        is the Form 4 sub-score and not the full Big Money Score, or a partial
        number gets compared against the design's weighting as though complete."""
        s = score_insiders([txn()], as_of=AS_OF)
        self.assertIn("13D/G", " ".join(s.components_absent))
        self.assertIn("13F", " ".join(s.components_absent))


class OtherSymbolTest(unittest.TestCase):
    def test_transactions_are_not_mixed_across_symbols(self):
        rows = [txn(), InsiderTransaction(
            symbol="OTHR", issuer_cik="999", owner_cik="0003", owner_name="C",
            transaction_date=AS_OF - timedelta(days=5),
            accepted_at=AS_OF - timedelta(days=3), code="P", acquired=True,
            shares=99_999, price=20.0, is_officer=True, accession="a9",
        )]
        s = score_insiders(rows, as_of=AS_OF, symbol="EXMP")
        self.assertEqual(s.purchase_count, 1)
        self.assertEqual(s.symbol, "EXMP")


if __name__ == "__main__":
    unittest.main()
