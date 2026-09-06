"""
Schedule 13D/13G parsing — two forms that look alike and share almost no field.

Fixtures are trimmed from real filings pulled 2026-09-06 (Addentax Group 13D,
Turtle Beach / 22NW 13G), not invented, because every field name below differs
between the two schemas and guessing them produced a parser that silently
returned nothing for one form.

The suite runs with no network and no key.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from qd.providers.schedules import (
    FilerClass,
    ScheduleKind,
    collapse_group,
    parse_schedule_13,
)
from qd.types import UTC

ACCEPTED = datetime(2026, 9, 2, 17, 4, 11, tzinfo=UTC)

# ── 13D: capital-D namespace, dateOfEvent, issuerCIK, percentOfClass ─────────
D_XML = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13D"
                 xmlns:com="http://www.sec.gov/edgar/common">
  <schemaVersion>X0202</schemaVersion>
  <headerData><submissionType>SCHEDULE 13D</submissionType></headerData>
  <formData>
    <coverPageHeader>
      <securitiesClassTitle>Common Stock, $0.001 par value</securitiesClassTitle>
      <dateOfEvent>08/19/2026</dateOfEvent>
      <issuerInfo>
        <issuerCIK>0001650101</issuerCIK>
        <issuerCusips><issuerCusipNumber>00653L400</issuerCusipNumber></issuerCusips>
        <issuerName>Addentax Group Corp.</issuerName>
      </issuerInfo>
    </coverPageHeader>
    <reportingPersons>
      <reportingPersonInfo>
        <reportingPersonCIK>0001776189</reportingPersonCIK>
        <reportingPersonName>HONG ZHIWANG</reportingPersonName>
        <soleVotingPower>225174.00</soleVotingPower>
        <sharedVotingPower>0.00</sharedVotingPower>
        <soleDispositivePower>225174.00</soleDispositivePower>
        <sharedDispositivePower>0.00</sharedDispositivePower>
        <aggregateAmountOwned>225174.00</aggregateAmountOwned>
        <percentOfClass>10.7</percentOfClass>
        <typeOfReportingPerson>IN</typeOfReportingPerson>
      </reportingPersonInfo>
    </reportingPersons>
  </formData>
</edgarSubmission>"""

# ── 13G: lowercase-g namespace, different names for every equivalent field ───
def g_xml(rule: str = "Rule 13d-1(c)", persons: str = "", submission: str = "SCHEDULE 13G") -> str:
    persons = persons or _g_person("22NW Fund, LP", "1304878.00", "7.29")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/schedule13g"
                 xmlns:com="http://www.sec.gov/edgar/common">
  <schemaVersion>X0202</schemaVersion>
  <headerData><submissionType>{submission}</submissionType></headerData>
  <formData>
    <coverPageHeader>
      <securitiesClassTitle>Common Stock, par value $0.001</securitiesClassTitle>
      <eventDateRequiresFilingThisStatement>08/27/2026</eventDateRequiresFilingThisStatement>
      <issuerInfo>
        <issuerCik>0001493761</issuerCik>
        <issuerName>Turtle Beach Corp</issuerName>
        <issuerCusips><issuerCusipNumber>900450206</issuerCusipNumber></issuerCusips>
      </issuerInfo>
      <designateRulesPursuantThisScheduleFiled>
        <designateRulePursuantThisScheduleFiled>{rule}</designateRulePursuantThisScheduleFiled>
      </designateRulesPursuantThisScheduleFiled>
    </coverPageHeader>
    {persons}
  </formData>
</edgarSubmission>"""


def _g_person(name: str, shares: str, pct: str, ptype: str = "PN") -> str:
    return f"""
    <coverPageHeaderReportingPersonDetails>
      <reportingPersonName>{name}</reportingPersonName>
      <citizenshipOrOrganization>DE</citizenshipOrOrganization>
      <reportingPersonBeneficiallyOwnedNumberOfShares>
        <soleVotingPower>{shares}</soleVotingPower>
        <sharedVotingPower>0.00</sharedVotingPower>
        <soleDispositivePower>{shares}</soleDispositivePower>
        <sharedDispositivePower>0.00</sharedDispositivePower>
      </reportingPersonBeneficiallyOwnedNumberOfShares>
      <reportingPersonBeneficiallyOwnedAggregateNumberOfShares>{shares}</reportingPersonBeneficiallyOwnedAggregateNumberOfShares>
      <classPercent>{pct}</classPercent>
      <typeOfReportingPerson>{ptype}</typeOfReportingPerson>
    </coverPageHeaderReportingPersonDetails>"""


def parse_d(xml: str = D_XML):
    return parse_schedule_13(xml, accepted_at=ACCEPTED, accession="0000-26-000001")


def parse_g(**kw):
    return parse_schedule_13(g_xml(**kw), accepted_at=ACCEPTED, accession="0000-26-000002")


class ThirteenDTest(unittest.TestCase):
    def test_parses(self):
        rows = parse_d()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.kind, ScheduleKind.D)
        self.assertEqual(r.issuer_name, "Addentax Group Corp.")
        self.assertEqual(r.issuer_cik, "0001650101")
        self.assertEqual(r.cusip, "00653L400")
        self.assertEqual(r.holder_name, "HONG ZHIWANG")
        self.assertEqual(r.holder_cik, "0001776189")
        self.assertAlmostEqual(r.percent_of_class, 10.7)
        self.assertAlmostEqual(r.shares, 225174.0)
        self.assertEqual(r.person_type, "IN")

    def test_a_13d_is_an_activist_filing(self):
        """13D means control intent. 13G is the passive box. The whole point of
        distinguishing them is that only one is a statement about the company's
        direction."""
        self.assertTrue(parse_d()[0].is_activist)
        self.assertFalse(parse_g()[0].is_activist)

    def test_13d_filer_class(self):
        self.assertEqual(parse_d()[0].filer_class, FilerClass.ACTIVIST)


class ThirteenGTest(unittest.TestCase):
    def test_parses_despite_sharing_no_field_names_with_13d(self):
        rows = parse_g()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r.kind, ScheduleKind.G)
        self.assertEqual(r.issuer_name, "Turtle Beach Corp")
        self.assertEqual(r.issuer_cik, "0001493761")
        self.assertEqual(r.holder_name, "22NW Fund, LP")
        self.assertAlmostEqual(r.percent_of_class, 7.29)
        self.assertAlmostEqual(r.shares, 1304878.0)

    def test_a_13g_filer_has_no_cik_in_the_schema(self):
        """13G carries no reportingPersonCIK, so holders can only be matched
        across filings by name. Recorded as empty rather than faked."""
        self.assertEqual(parse_g()[0].holder_cik, "")

    def test_voting_power_is_nested_one_level_deeper_than_on_13d(self):
        r = parse_g()[0]
        self.assertAlmostEqual(r.sole_voting, 1304878.0)
        self.assertAlmostEqual(r.shared_voting, 0.0)


class NamespaceTest(unittest.TestCase):
    """13D declares .../schedule13D and 13G declares .../schedule13g — the same
    word with different casing. Anything matching namespaces literally handles
    one form and silently returns nothing for the other."""

    def test_both_namespaces_parse(self):
        self.assertEqual(len(parse_d()), 1)
        self.assertEqual(len(parse_g()), 1)

    def test_an_unknown_future_namespace_still_parses(self):
        """Schema versions change. Matching on local element names rather than
        fully-qualified ones means a new namespace is not a silent outage."""
        rows = parse_schedule_13(
            D_XML.replace("http://www.sec.gov/edgar/schedule13D",
                          "http://www.sec.gov/edgar/schedule13D/2030"),
            accepted_at=ACCEPTED,
        )
        self.assertEqual(len(rows), 1)


class FilerClassTest(unittest.TestCase):
    """The rule cited on a 13G determines the filer class, and the filer class
    determines the DISCLOSURE LAG. Reading it from the filing beats assuming a
    single lag across all history."""

    def test_passive_investor(self):
        self.assertEqual(parse_g(rule="Rule 13d-1(c)")[0].filer_class, FilerClass.PASSIVE)

    def test_qualified_institutional_investor(self):
        self.assertEqual(parse_g(rule="Rule 13d-1(b)")[0].filer_class, FilerClass.QII)

    def test_exempt_investor(self):
        self.assertEqual(parse_g(rule="Rule 13d-1(d)")[0].filer_class, FilerClass.EXEMPT)

    def test_an_unrecognised_rule_is_unknown_not_guessed(self):
        self.assertEqual(parse_g(rule="Rule 13d-9(z)")[0].filer_class, FilerClass.UNKNOWN)

    def test_a_passive_filer_reports_faster_than_an_institutional_one(self):
        """13d-1(c) is days; 13d-1(b) is up to 45 days after quarter end. A
        score that treats them alike misdates the slower one badly."""
        passive = parse_g(rule="Rule 13d-1(c)")[0]
        qii = parse_g(rule="Rule 13d-1(b)")[0]
        self.assertLess(passive.max_disclosure_lag_days, qii.max_disclosure_lag_days)


class TheTwoTimestampsTest(unittest.TestCase):
    def test_event_date_parses_from_us_format(self):
        """13D/G dates are MM/DD/YYYY, unlike Form 4's YYYY-MM-DD. Read as ISO
        they would either fail or, worse, silently transpose month and day."""
        self.assertEqual(parse_d()[0].event_date.date().isoformat(), "2026-08-19")
        self.assertEqual(parse_g()[0].event_date.date().isoformat(), "2026-08-27")

    def test_known_at_is_the_acceptance_instant(self):
        self.assertEqual(parse_d()[0].known_at, ACCEPTED)

    def test_event_precedes_disclosure(self):
        r = parse_d()[0]
        self.assertLess(r.event_time, r.known_at)

    def test_an_event_after_acceptance_is_refused(self):
        bad = D_XML.replace("<dateOfEvent>08/19/2026</dateOfEvent>",
                            "<dateOfEvent>12/01/2026</dateOfEvent>")
        self.assertEqual(parse_d(bad), [])


class GroupDoubleCountTest(unittest.TestCase):
    """The trap this module exists for.

    A fund files jointly with its GP, its manager and its principal. All four
    report the SAME shares — 22NW's real filing does exactly this. Summing
    aggregate holdings across reporting persons quadruples the position, and
    the resulting "accumulation" is an artefact of the filing structure rather
    than anything anyone bought.
    """

    def _joint(self):
        persons = "".join(
            _g_person(n, "1304878.00", "7.29")
            for n in ("22NW Fund, LP", "22NW, LP", "22NW GP, Inc.", "English Aron R")
        )
        return parse_g(persons=persons)

    def test_each_reporting_person_is_parsed(self):
        self.assertEqual(len(self._joint()), 4)

    def test_collapsing_the_group_yields_one_position(self):
        self.assertEqual(len(collapse_group(self._joint())), 1)

    def test_the_collapsed_position_is_not_multiplied(self):
        collapsed = collapse_group(self._joint())[0]
        self.assertAlmostEqual(collapsed.shares, 1304878.0)
        self.assertAlmostEqual(collapsed.percent_of_class, 7.29)

    def test_genuinely_different_holders_are_not_collapsed(self):
        """Two unrelated funds each holding 6% is real breadth, and must
        survive the same routine that collapses a joint filing."""
        rows = parse_g(persons=_g_person("Fund A", "1000000.00", "6.0")) + \
            parse_schedule_13(g_xml(persons=_g_person("Fund B", "900000.00", "5.4")),
                              accepted_at=ACCEPTED, accession="different-accession")
        self.assertEqual(len(collapse_group(rows)), 2)

    def test_collapse_keeps_the_largest_reported_stake(self):
        """Group members sometimes report slightly different totals. Keeping the
        largest avoids understating a position that is genuinely held."""
        persons = (_g_person("Fund", "1304878.00", "7.29")
                   + _g_person("GP", "1300000.00", "7.26"))
        self.assertAlmostEqual(collapse_group(parse_g(persons=persons))[0].shares, 1304878.0)


class AmendmentTest(unittest.TestCase):
    def test_an_amendment_is_flagged(self):
        self.assertTrue(parse_g(submission="SCHEDULE 13G/A")[0].is_amendment)

    def test_an_original_is_not(self):
        self.assertFalse(parse_g()[0].is_amendment)

    def test_an_exit_amendment_is_recognised(self):
        """A 13G/A reporting 0% is a holder LEAVING — the opposite signal, and
        one that reads as an ordinary filing if only the percentage is stored."""
        rows = parse_g(submission="SCHEDULE 13G/A",
                       persons=_g_person("22NW Fund, LP", "0.00", "0.0"))
        self.assertTrue(rows[0].is_exit)
        self.assertFalse(parse_g()[0].is_exit)


class ThresholdTest(unittest.TestCase):
    def test_a_five_percent_stake_crosses(self):
        self.assertTrue(parse_g()[0].crosses_threshold)

    def test_a_residual_stake_does_not(self):
        rows = parse_g(persons=_g_person("Fund", "100.00", "0.4"))
        self.assertFalse(rows[0].crosses_threshold)


class RobustnessTest(unittest.TestCase):
    def test_malformed_xml_returns_empty(self):
        self.assertEqual(parse_schedule_13("<edgarSubmission><oops>",
                                           accepted_at=ACCEPTED), [])

    def test_an_unstructured_pre_2024_document_returns_empty(self):
        """Filings before the structured mandate are HTML or plain text with no
        primary_doc.xml. They must return nothing rather than raise — but the
        CALLER has to know the difference between 'no 5% holders' and 'this era
        has no machine-readable filings', which is what has_structured_data is
        for."""
        html = b"<html><body>SCHEDULE 13G ... Percent of class: 7.2%</body></html>"
        self.assertEqual(parse_schedule_13(html.decode(), accepted_at=ACCEPTED), [])

    def test_a_missing_percentage_is_none_not_zero(self):
        """Zero would read as a holder who has exited."""
        stripped = D_XML.replace("<percentOfClass>10.7</percentOfClass>", "")
        self.assertIsNone(parse_d(stripped)[0].percent_of_class)


if __name__ == "__main__":
    unittest.main()
