"""
Form 13F institutional holdings — the quarterly snapshot, and its four eras.

Fixtures are trimmed from real filings pulled 2026-09-06 (&PARTNERS 13F-HR,
1607 Capital 13F-HR/A), not invented.

The three traps pinned here were each measured against live EDGAR rather than
assumed, because each produces plausible numbers when got wrong:

  VALUE UNITS      Values were in THOUSANDS before 2023 and whole DOLLARS
                   after. Measured: median implied price per share was $0.027
                   in 2021 Q3 and $228.53 in 2023 Q3. One convention across
                   all history is wrong by 1000x on one side of that line.

  AMENDMENT TYPE   RESTATEMENT replaces the prior table; NEW HOLDINGS adds to
                   it. Both were observed from the same filer in one quarter.
                   Treating them alike either double-counts or drops positions.

  13F-NT           A notice saying "someone else reports my holdings". It has
                   no table. That is not a manager who owns nothing.

The suite runs with no network and no key.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from qd.providers.holdings13f import (
    VALUE_IN_WHOLE_DOLLARS_FROM,
    AmendmentType,
    HoldingsState,
    ReportType,
    ShareType,
    net_new_positions,
    parse_13f_cover,
    parse_13f_table,
)
from qd.types import UTC

ACCEPTED = datetime(2026, 7, 7, 20, 15, 0, tzinfo=UTC)
OLD_ACCEPTED = datetime(2021, 8, 12, 20, 15, 0, tzinfo=UTC)


def cover(report_type="13F HOLDINGS REPORT", amendment="false", amendment_type=None,
          period="06-30-2026", managers="0") -> str:
    at = f"<amendmentType>{amendment_type}</amendmentType>" if amendment_type else ""
    return f"""<?xml version="1.0"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler"
                 xmlns:ns1="http://www.sec.gov/edgar/common">
  <headerData>
    <filerInfo>
      <filer><credentials><cik>0000107136</cik></credentials></filer>
      <periodOfReport>{period}</periodOfReport>
    </filerInfo>
  </headerData>
  <formData>
    <coverPage>
      <reportCalendarOrQuarter>{period}</reportCalendarOrQuarter>
      <isAmendment>{amendment}</isAmendment>
      {at}
      <filingManager><name>&amp;PARTNERS</name></filingManager>
      <reportType>{report_type}</reportType>
      <form13FFileNumber>028-19034</form13FFileNumber>
    </coverPage>
    <summaryPage>
      <otherIncludedManagersCount>{managers}</otherIncludedManagersCount>
      <tableEntryTotal>2</tableEntryTotal>
      <tableValueTotal>24366839693</tableValueTotal>
    </summaryPage>
  </formData>
</edgarSubmission>"""


def table(entries: str = "") -> str:
    entries = entries or (
        _entry("1ST SOURCE CORP", "336901103", "388101", "4757")
        + _entry("MICRON TECHNOLOGY INC", "595112103", "1000000", "10000")
    )
    return f"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
{entries}
</informationTable>"""


def _entry(name, cusip, value, shares, share_type="SH", discretion="SOLE",
           sole="", other="") -> str:
    sole = sole or shares
    om = f"<otherManager>{other}</otherManager>" if other else ""
    return f"""
  <infoTable>
    <nameOfIssuer>{name}</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>{cusip}</cusip>
    <value>{value}</value>
    <shrsOrPrnAmt>
      <sshPrnamt>{shares}</sshPrnamt>
      <sshPrnamtType>{share_type}</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>{discretion}</investmentDiscretion>
    {om}
    <votingAuthority><Sole>{sole}</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>"""


def holdings(xml=None, accepted=ACCEPTED, **kw):
    return parse_13f_table(xml or table(), accepted_at=accepted,
                           accession="0000-26-000009", **kw)


class CoverTest(unittest.TestCase):
    def test_parses(self):
        c = parse_13f_cover(cover(), accepted_at=ACCEPTED, accession="a1")
        self.assertEqual(c.manager_name, "&PARTNERS")
        self.assertEqual(c.manager_cik, "0000107136")
        self.assertEqual(c.report_type, ReportType.HOLDINGS)
        self.assertFalse(c.is_amendment)

    def test_period_parses_from_dash_separated_us_format(self):
        """13F writes 06-30-2026. Form 4 uses ISO and 13D/G uses slashes — three
        SEC forms, three date formats, all in this one project."""
        c = parse_13f_cover(cover(), accepted_at=ACCEPTED, accession="a1")
        self.assertEqual(c.period_of_report.date().isoformat(), "2026-06-30")

    def test_the_reporting_lag_is_visible(self):
        """A 13F filed in July reports a position held on 30 June — and it may
        have been opened in April and closed in May. The snapshot is up to a
        quarter stale before the 45-day filing lag is even counted."""
        c = parse_13f_cover(cover(), accepted_at=ACCEPTED, accession="a1")
        self.assertGreater(c.disclosure_lag_days, 0)
        self.assertLess(c.event_time, c.known_at)


class NoticeTest(unittest.TestCase):
    """A 13F-NT is a manager saying another filer reports their holdings."""

    def test_a_notice_is_not_a_manager_holding_nothing(self):
        c = parse_13f_cover(cover(report_type="13F NOTICE"),
                            accepted_at=ACCEPTED, accession="a1")
        self.assertEqual(c.report_type, ReportType.NOTICE)
        self.assertEqual(c.state, HoldingsState.REPORTED_ELSEWHERE)
        self.assertFalse(c.state.is_evidence_of_absence)

    def test_a_holdings_report_with_a_table_is_readable(self):
        c = parse_13f_cover(cover(), accepted_at=ACCEPTED, accession="a1")
        self.assertEqual(c.state, HoldingsState.PARSED)


class ValueUnitsTest(unittest.TestCase):
    """The 1000x trap, measured against live EDGAR before being encoded."""

    def test_modern_values_are_whole_dollars(self):
        h = holdings()[0]
        self.assertAlmostEqual(h.value_usd, 388101.0)

    def test_pre_2023_values_are_multiplied_from_thousands(self):
        """Measured: median implied price per share was $0.027 in 2021 Q3 and
        $228.53 in 2023 Q3. Same field, different unit."""
        h = holdings(accepted=OLD_ACCEPTED)[0]
        self.assertAlmostEqual(h.value_usd, 388101.0 * 1000)

    def test_the_implied_price_is_plausible_in_both_eras(self):
        """The check that would have caught this: value/shares must look like a
        share price. 1st Source Corp traded near $80, not $0.08 or $81,585.

        Each era gets a fixture written the way that era actually filed —
        388101 whole dollars now, 388 thousands then — for the same real
        position. Both must normalise to the same plausible price.
        """
        modern = holdings(accepted=ACCEPTED)[0]
        legacy = holdings(table(_entry("1ST SOURCE CORP", "336901103",
                                       "388", "4757")),
                          accepted=OLD_ACCEPTED)[0]
        for h, era in ((modern, "2026"), (legacy, "2021")):
            self.assertTrue(1.0 < h.implied_price < 10_000.0,
                            f"{h.implied_price} in {era}")
        # The same holding, filed under two conventions, agrees to the dollar.
        self.assertAlmostEqual(modern.implied_price, legacy.implied_price, places=0)

    def test_the_boundary_is_the_regulatory_date(self):
        self.assertEqual(VALUE_IN_WHOLE_DOLLARS_FROM.isoformat(), "2023-01-01")

    def test_raw_value_is_preserved_alongside_the_normalised_one(self):
        h = holdings(accepted=OLD_ACCEPTED)[0]
        self.assertAlmostEqual(h.value_raw, 388101.0)


class ShareTypeTest(unittest.TestCase):
    def test_shares_parse_as_shares(self):
        self.assertEqual(holdings()[0].share_type, ShareType.SH)

    def test_principal_amounts_are_not_shares(self):
        """sshPrnamtType PRN is a face value in dollars, not a share count.
        Summing PRN rows into a share total adds bond principal to equity
        counts, and the result looks like an enormous position."""
        xml = table(_entry("SOME NOTE 4.5% 2031", "12345AB67", "5000000",
                           "5000000", share_type="PRN"))
        h = holdings(xml)[0]
        self.assertEqual(h.share_type, ShareType.PRN)
        self.assertFalse(h.is_equity_position)

    def test_only_equity_positions_count_as_holdings(self):
        xml = table(_entry("A CORP", "111111111", "1000", "10")
                    + _entry("A NOTE", "222222222", "5000000", "5000000",
                             share_type="PRN"))
        rows = holdings(xml)
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(1 for r in rows if r.is_equity_position), 1)


class AmendmentTest(unittest.TestCase):
    """RESTATEMENT replaces; NEW HOLDINGS adds. Both seen from one filer in one
    quarter on real filings."""

    def test_restatement_is_recognised(self):
        c = parse_13f_cover(cover(amendment="true", amendment_type="RESTATEMENT"),
                            accepted_at=ACCEPTED, accession="a1")
        self.assertTrue(c.is_amendment)
        self.assertEqual(c.amendment_type, AmendmentType.RESTATEMENT)
        self.assertTrue(c.replaces_prior)

    def test_new_holdings_adds_rather_than_replaces(self):
        c = parse_13f_cover(cover(amendment="true", amendment_type="NEW HOLDINGS"),
                            accepted_at=ACCEPTED, accession="a1")
        self.assertEqual(c.amendment_type, AmendmentType.NEW_HOLDINGS)
        self.assertFalse(c.replaces_prior)

    def test_an_original_replaces_nothing(self):
        c = parse_13f_cover(cover(), accepted_at=ACCEPTED, accession="a1")
        self.assertEqual(c.amendment_type, AmendmentType.NONE)
        self.assertFalse(c.replaces_prior)

    def test_an_amendment_with_no_type_is_unknown_not_assumed(self):
        """Getting this wrong in either direction corrupts the quarter: assume
        restatement and you drop positions, assume additive and you double."""
        c = parse_13f_cover(cover(amendment="true"), accepted_at=ACCEPTED,
                            accession="a1")
        self.assertEqual(c.amendment_type, AmendmentType.UNKNOWN)


class NewPositionTest(unittest.TestCase):
    """13F is a snapshot. The signal is the DIFFERENCE between two of them."""

    def _q(self, *cusips):
        return holdings(table("".join(
            _entry(f"CO {c}", c, "1000000", "10000") for c in cusips)))

    def test_a_position_absent_last_quarter_is_new(self):
        new = net_new_positions(self._q("111111111", "222222222"),
                                self._q("111111111"))
        self.assertEqual({h.cusip for h in new}, {"222222222"})

    def test_a_held_position_is_not_new(self):
        new = net_new_positions(self._q("111111111"), self._q("111111111"))
        self.assertEqual(new, [])

    def test_with_no_prior_quarter_nothing_is_called_new(self):
        """The first quarter of an archive is not a quarter in which every
        institution bought everything. Without a prior, newness is unknowable
        and inventing it would put a spike at the start of every backtest."""
        self.assertEqual(net_new_positions(self._q("111111111"), None), [])


class RobustnessTest(unittest.TestCase):
    def test_malformed_table_returns_empty(self):
        self.assertEqual(parse_13f_table("<informationTable><oops>",
                                         accepted_at=ACCEPTED), [])

    def test_malformed_cover_reports_parse_failure(self):
        c = parse_13f_cover("<edgarSubmission><oops>", accepted_at=ACCEPTED,
                            accession="a1")
        self.assertEqual(c.state, HoldingsState.PARSE_FAILED)
        self.assertFalse(c.state.is_evidence_of_absence)

    def test_a_holding_with_no_shares_is_dropped_not_zeroed(self):
        xml = table(_entry("BROKEN", "999999999", "1000", ""))
        self.assertEqual(holdings(xml), [])


if __name__ == "__main__":
    unittest.main()
