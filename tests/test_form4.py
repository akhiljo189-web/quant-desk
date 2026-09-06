"""
Form 4 parsing, and the six ways it silently becomes a compensation tracker.

The insider hypothesis rests on OPEN-MARKET PURCHASES. Roughly nine in ten
Form 4 rows are not that — they are grants, option exercises, tax withholding
and pre-planned sales — and every one of them parses cleanly, produces a
plausible share count, and is marked "acquired". A parser that takes the rows
at face value measures how much stock a company pays its executives, which
correlates with size and with nothing useful.

These tests pin the exclusions. All fixtures are literal document shapes, so
the suite runs with no network and no key.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from qd.providers.forms import (
    InsiderTransaction,
    Seniority,
    parse_form4,
)
from qd.types import UTC

ACCEPTED = datetime(2026, 2, 12, 21, 30, 28, tzinfo=UTC)


def _doc(body: str, *, period: str = "2026-02-10", form_type: str = "4") -> str:
    return f"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>{form_type}</documentType>
  <periodOfReport>{period}</periodOfReport>
  <issuer>
    <issuerCik>0000320193</issuerCik>
    <issuerName>Example Inc</issuerName>
    <issuerTradingSymbol>EXMP</issuerTradingSymbol>
  </issuer>
  {body}
</ownershipDocument>"""


def _owner(
    name: str = "DOE JANE",
    cik: str = "0001111111",
    *,
    director: str = "0",
    officer: str = "1",
    ten_pct: str = "0",
    title: str = "Chief Executive Officer",
) -> str:
    return f"""
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>{cik}</rptOwnerCik>
      <rptOwnerName>{name}</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>{director}</isDirector>
      <isOfficer>{officer}</isOfficer>
      <isTenPercentOwner>{ten_pct}</isTenPercentOwner>
      <isOther>0</isOther>
      <officerTitle>{title}</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>"""


def _nd_txn(
    code: str = "P",
    *,
    ad: str = "A",
    shares: str = "10000",
    price: str = "185.50",
    date: str = "2026-02-10",
    owned_after: str = "328000",
    coding_extra: str = "",
) -> str:
    price_block = (
        f"""<transactionPricePerShare><value>{price}</value></transactionPricePerShare>"""
        if price is not None
        else "<transactionPricePerShare/>"
    )
    return f"""
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>{date}</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>{code}</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
        {coding_extra}
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>{shares}</value></transactionShares>
        {price_block}
        <transactionAcquiredDisposedCode><value>{ad}</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>{owned_after}</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>"""


def parse(body: str, **kw) -> list[InsiderTransaction]:
    return parse_form4(_doc(body, **kw), accepted_at=ACCEPTED, accession="0000-25-000001")


class ParseTest(unittest.TestCase):
    def test_open_market_purchase_parses(self):
        txns = parse(_owner() + _nd_txn("P"))
        self.assertEqual(len(txns), 1)
        t = txns[0]
        self.assertEqual(t.symbol, "EXMP")
        self.assertEqual(t.owner_name, "DOE JANE")
        self.assertEqual(t.code, "P")
        self.assertTrue(t.acquired)
        self.assertAlmostEqual(t.shares, 10000.0)
        self.assertAlmostEqual(t.price, 185.50)
        self.assertTrue(t.is_open_market_purchase)

    def test_value_is_shares_times_price(self):
        t = parse(_owner() + _nd_txn("P", shares="1000", price="20"))[0]
        self.assertAlmostEqual(t.value, 20000.0)

    def test_missing_price_gives_no_value_rather_than_zero(self):
        """A zero value would sort as the smallest purchase rather than as the
        unknown it is, and would silently dilute any size weighting."""
        t = parse(_owner() + _nd_txn("P", price=None))[0]
        self.assertIsNone(t.price)
        self.assertIsNone(t.value)

    def test_a_document_with_no_transactions_yields_nothing(self):
        self.assertEqual(parse(_owner()), [])

    def test_malformed_xml_returns_empty_not_an_exception(self):
        """One bad document in a bulk pull must not end the run."""
        self.assertEqual(
            parse_form4("<ownershipDocument><unclosed>", accepted_at=ACCEPTED), []
        )


class TheTwoTimestampsTest(unittest.TestCase):
    """Form 4 is due two business days after the trade. Those are two different
    instants and conflating them hands the backtest a two-day head start on
    every insider trade in history."""

    def test_event_time_is_the_transaction_date(self):
        t = parse(_owner() + _nd_txn("P", date="2026-02-10"))[0]
        self.assertEqual(t.event_time.date().isoformat(), "2026-02-10")

    def test_known_at_is_the_filing_acceptance_instant(self):
        t = parse(_owner() + _nd_txn("P", date="2026-02-10"))[0]
        self.assertEqual(t.known_at, ACCEPTED)

    def test_known_at_is_strictly_after_the_trade(self):
        t = parse(_owner() + _nd_txn("P", date="2026-02-10"))[0]
        self.assertGreater(t.known_at, t.event_time)

    def test_a_transaction_date_after_acceptance_is_refused(self):
        """A trade cannot post-date the filing that reports it. Such a row is
        corrupt, and keeping it would create a record knowable before it
        happened."""
        self.assertEqual(parse(_owner() + _nd_txn("P", date="2026-03-01")), [])


class TransactionCodeTest(unittest.TestCase):
    """The whole module in one class. Every code below marks shares ACQUIRED,
    and only one of them is a purchase."""

    def test_grant_is_not_a_purchase(self):
        """Code A — a compensation award. Free stock. Counting it as buying
        turns the score into a measure of executive pay."""
        t = parse(_owner() + _nd_txn("A", price="0"))[0]
        self.assertTrue(t.acquired)
        self.assertFalse(t.is_open_market_purchase)

    def test_option_exercise_is_not_a_purchase(self):
        """Code M — exercising a grant received years earlier. It says nothing
        about today's view of the price; it usually says the option is expiring."""
        t = parse(_owner() + _nd_txn("M", price="12.00"))[0]
        self.assertTrue(t.acquired)
        self.assertFalse(t.is_open_market_purchase)

    def test_tax_withholding_is_not_a_sale_signal(self):
        """Code F — shares surrendered to cover tax on a vest. Automatic, and
        it appears on the calendar of every vesting schedule in the market."""
        t = parse(_owner() + _nd_txn("F", ad="D", price="185.50"))[0]
        self.assertFalse(t.is_open_market_sale)

    def test_gift_is_neither(self):
        t = parse(_owner() + _nd_txn("G", price="0"))[0]
        self.assertFalse(t.is_open_market_purchase)
        self.assertFalse(t.is_open_market_sale)

    def test_open_market_sale_is_a_sale(self):
        t = parse(_owner() + _nd_txn("S", ad="D"))[0]
        self.assertTrue(t.is_open_market_sale)
        self.assertFalse(t.is_open_market_purchase)

    def test_acquired_flag_alone_does_not_make_a_purchase(self):
        """The trap this class exists for. `transactionAcquiredDisposedCode`
        is A for a grant, an exercise, a gift received AND a purchase. Filtering
        on it instead of on the transaction code captures the entire
        compensation stream."""
        acquired_but_not_bought = [
            parse(_owner() + _nd_txn(code, ad="A"))[0] for code in ("A", "M", "G", "C")
        ]
        self.assertTrue(all(t.acquired for t in acquired_but_not_bought))
        self.assertFalse(any(t.is_open_market_purchase for t in acquired_but_not_bought))

    def test_a_purchase_at_zero_price_is_not_an_open_market_purchase(self):
        """Open-market means money changed hands. A P coded at zero is a
        mis-tagged transfer."""
        t = parse(_owner() + _nd_txn("P", price="0"))[0]
        self.assertFalse(t.is_open_market_purchase)

    def test_unknown_code_is_neither_signal(self):
        t = parse(_owner() + _nd_txn("Z"))[0]
        self.assertFalse(t.is_open_market_purchase)
        self.assertFalse(t.is_open_market_sale)


class Rule10b51Test(unittest.TestCase):
    """A trade scheduled months in advance carries no view. The SEC only added
    a structured flag in 2023; before that the fact lives in footnote prose,
    so both paths have to work or the exclusion silently stops applying to the
    older half of the sample — the half with the most history."""

    def test_structured_flag_is_read(self):
        t = parse(
            _owner() + _nd_txn("S", ad="D", coding_extra="<aff10b5One>1</aff10b5One>")
        )[0]
        self.assertTrue(t.planned_10b5_1)
        self.assertFalse(t.is_open_market_sale)

    def test_structured_flag_false_leaves_the_trade_readable(self):
        """The flag is present and 0 on most post-2023 filings. Reading its
        presence rather than its value would exclude the entire recent sample."""
        t = parse(
            _owner() + _nd_txn("S", ad="D", coding_extra="<aff10b5One>0</aff10b5One>")
        )[0]
        self.assertFalse(t.planned_10b5_1)
        self.assertTrue(t.is_open_market_sale)

    def test_footnote_prose_is_read(self):
        body = _owner() + _nd_txn("S", ad="D") + """
  <footnotes>
    <footnote id="F1">This sale was effected pursuant to a Rule 10b5-1 trading
    plan adopted by the reporting person on August 2, 2025.</footnote>
  </footnotes>"""
        t = parse(body)[0]
        self.assertTrue(t.planned_10b5_1)

    def test_a_planned_purchase_is_also_excluded(self):
        """Symmetry matters. A planned buy is as uninformative as a planned
        sale, and pre-planned buying programmes exist."""
        body = _owner() + _nd_txn("P") + """
  <footnotes>
    <footnote id="F1">Purchased under a Rule 10b5-1 plan.</footnote>
  </footnotes>"""
        t = parse(body)[0]
        self.assertTrue(t.planned_10b5_1)
        self.assertFalse(t.is_open_market_purchase)

    def test_ordinary_footnotes_do_not_trip_the_flag(self):
        body = _owner() + _nd_txn("P") + """
  <footnotes>
    <footnote id="F1">Shares held in a family trust of which the reporting
    person is a co-trustee.</footnote>
  </footnotes>"""
        self.assertFalse(parse(body)[0].planned_10b5_1)


class SeniorityTest(unittest.TestCase):
    def test_ceo_is_recognised_from_a_free_text_title(self):
        t = parse(_owner(title="Chief Executive Officer") + _nd_txn("P"))[0]
        self.assertEqual(t.seniority, Seniority.CEO)

    def test_president_and_ceo_reads_as_ceo(self):
        t = parse(_owner(title="President and CEO") + _nd_txn("P"))[0]
        self.assertEqual(t.seniority, Seniority.CEO)

    def test_cfo_is_recognised(self):
        t = parse(_owner(title="Chief Financial Officer") + _nd_txn("P"))[0]
        self.assertEqual(t.seniority, Seniority.CFO)

    def test_other_officer_titles_fall_back_to_officer(self):
        t = parse(_owner(title="Chief Marketing Officer") + _nd_txn("P"))[0]
        self.assertEqual(t.seniority, Seniority.OFFICER)

    def test_director_without_an_officer_role(self):
        t = parse(_owner(director="1", officer="0", title="") + _nd_txn("P"))[0]
        self.assertEqual(t.seniority, Seniority.DIRECTOR)

    def test_ten_percent_owner_is_ranked_lowest(self):
        """A sponsor or index fund crossing 5% trades for portfolio reasons.
        It is not an insider's view of the business."""
        t = parse(
            _owner(director="0", officer="0", ten_pct="1", title="") + _nd_txn("P")
        )[0]
        self.assertEqual(t.seniority, Seniority.TEN_PERCENT)

    def test_an_officer_who_is_also_a_director_keeps_the_officer_rank(self):
        t = parse(_owner(director="1", officer="1", title="Chief Financial Officer") + _nd_txn("P"))[0]
        self.assertEqual(t.seniority, Seniority.CFO)


class DerivativeTableTest(unittest.TestCase):
    def test_derivative_rows_are_flagged_and_excluded_from_purchases(self):
        """An option exercise appears twice — once in the derivative table as
        the option disposed, once in the non-derivative table as the shares
        acquired. Counting both double-counts a transaction that is not a
        purchase either way."""
        body = _owner() + """
  <derivativeTable>
    <derivativeTransaction>
      <securityTitle><value>Employee Stock Option</value></securityTitle>
      <transactionDate><value>2026-02-10</value></transactionDate>
      <transactionCoding>
        <transactionFormType>4</transactionFormType>
        <transactionCode>M</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </derivativeTransaction>
  </derivativeTable>"""
        txns = parse(body)
        self.assertEqual(len(txns), 1)
        self.assertTrue(txns[0].is_derivative)
        self.assertFalse(txns[0].is_open_market_purchase)


class AmendmentTest(unittest.TestCase):
    def test_an_amendment_is_flagged(self):
        """Form 4/A restates an earlier filing. The correction was not
        available when the original was filed — the same restatement trap the
        XBRL adapter handles, arriving through a different door."""
        txns = parse_form4(
            _doc(_owner() + _nd_txn("P"), form_type="4/A"), accepted_at=ACCEPTED
        )
        self.assertTrue(txns[0].is_amendment)


class MultipleOwnerTest(unittest.TestCase):
    def test_each_reporting_owner_yields_its_own_row(self):
        body = (
            _owner("DOE JANE", "0001111111")
            + _owner("DOE JOHN", "0002222222", title="Chief Financial Officer")
            + _nd_txn("P")
        )
        txns = parse(body)
        self.assertEqual(len(txns), 2)
        self.assertEqual({t.owner_name for t in txns}, {"DOE JANE", "DOE JOHN"})

    def test_a_joint_filing_shares_one_accession(self):
        """So the scorer can tell two people who each filed from two people
        named on one filing — a family trust is one decision, not a cluster."""
        body = _owner("DOE JANE", "0001111111") + _owner("DOE JOHN", "0002222222") + _nd_txn("P")
        txns = parse(body)
        self.assertEqual(len({t.accession for t in txns}), 1)


class ExclusionReasonTest(unittest.TestCase):
    """The parser must be able to account for what it dropped. A purchase count
    with no denominator hides a parser that is quietly rejecting everything."""

    def test_a_purchase_has_no_exclusion_reason(self):
        self.assertIsNone(parse(_owner() + _nd_txn("P"))[0].exclusion_reason)

    def test_a_sale_has_no_exclusion_reason(self):
        self.assertIsNone(parse(_owner() + _nd_txn("S", ad="D"))[0].exclusion_reason)

    def test_a_grant_is_reported_as_compensation(self):
        self.assertEqual(
            parse(_owner() + _nd_txn("A", price="0"))[0].exclusion_reason,
            "compensation",
        )

    def test_a_gift_is_reported_as_a_transfer(self):
        self.assertEqual(
            parse(_owner() + _nd_txn("G", price="0"))[0].exclusion_reason, "transfer"
        )

    def test_a_planned_sale_is_reported_as_such(self):
        t = parse(
            _owner() + _nd_txn("S", ad="D", coding_extra="<aff10b5One>1</aff10b5One>")
        )[0]
        self.assertEqual(t.exclusion_reason, "Rule 10b5-1 plan")

    def test_a_priceless_purchase_says_so(self):
        self.assertEqual(
            parse(_owner() + _nd_txn("P", price=None))[0].exclusion_reason,
            "no price disclosed",
        )

    def test_an_unknown_code_is_named_not_swallowed(self):
        """A code the SEC adds later must surface as unrecognised rather than
        joining whichever bucket a catch-all would have put it in."""
        reason = parse(_owner() + _nd_txn("Z"))[0].exclusion_reason
        self.assertIn("unrecognised", reason)
        self.assertIn("Z", reason)


if __name__ == "__main__":
    unittest.main()
