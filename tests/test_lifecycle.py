"""
Company lifecycle — did this company die, when, and why.

The survivorship gate from the design's §3, §15 and §17. A universe missing its
failures turns "insider buying predicts returns" into "insider buying at
companies that survived predicts returns", which is true and worthless.

The classification is point-in-time: a company that died in 2020 was alive in
2015, and asking "is this company dead" must be answered with what was knowable
on the asking date. Getting that wrong deletes companies from the universe
years before anything happened to them.

Runs with no network and no key.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.providers.edgar import Filing
from qd.providers.lifecycle import (
    UNKNOWN_REASON_GATE,
    DelistingReason,
    LifecycleStatus,
    Scenario,
    classify_lifecycle,
    cohort_report,
    delisting_return,
)
from qd.types import UTC

NOW = datetime(2026, 9, 1, tzinfo=UTC)


def filing(form: str, when: datetime, items: tuple[str, ...] = ()) -> Filing:
    return Filing(symbol="TEST", cik="0000000001", form=form,
                  filed_date=when, accepted_at=when, items=items)


def quarterly(start: datetime, n: int) -> list[Filing]:
    """A normal filing cadence: 10-Qs every quarter, a 10-K each year."""
    out = []
    for i in range(n):
        when = start + timedelta(days=91 * i)
        out.append(filing("10-K" if i % 4 == 0 else "10-Q", when))
    return out


class AliveTest(unittest.TestCase):
    def test_a_company_filing_normally_is_alive(self):
        lc = classify_lifecycle(quarterly(NOW - timedelta(days=730), 8), as_of=NOW)
        self.assertEqual(lc.status, LifecycleStatus.ALIVE)
        self.assertIsNone(lc.death_date)

    def test_no_filings_at_all_is_unknown_not_dead(self):
        lc = classify_lifecycle([], as_of=NOW)
        self.assertEqual(lc.status, LifecycleStatus.UNKNOWN)


class PointInTimeTest(unittest.TestCase):
    """A company that died in 2020 was alive in 2015."""

    def _died_2020(self):
        return quarterly(datetime(2015, 1, 15, tzinfo=UTC), 20) + [
            filing("8-K", datetime(2020, 6, 1, tzinfo=UTC), items=("1.03",)),
        ]

    def test_it_is_alive_before_it_died(self):
        lc = classify_lifecycle(self._died_2020(),
                                as_of=datetime(2017, 1, 1, tzinfo=UTC))
        self.assertEqual(lc.status, LifecycleStatus.ALIVE)

    def test_it_is_dead_after(self):
        lc = classify_lifecycle(self._died_2020(),
                                as_of=datetime(2021, 1, 1, tzinfo=UTC))
        self.assertEqual(lc.status, LifecycleStatus.DEAD)
        self.assertEqual(lc.reason, DelistingReason.BANKRUPTCY_DISTRESS)

    def test_a_future_filing_is_never_visible(self):
        """The whole point. Using the 2020 bankruptcy to mark the company dead
        in 2017 would delete it from the universe three years before anything
        happened — and it would delete exactly the companies that went on to
        fail, which is the survivorship bias inverted but no less fatal."""
        lc = classify_lifecycle(self._died_2020(),
                                as_of=datetime(2017, 1, 1, tzinfo=UTC))
        self.assertIsNone(lc.death_date)
        self.assertEqual(lc.reason, DelistingReason.NONE)


class ReasonTest(unittest.TestCase):
    """8-K item 1.03 is a legally-mandated bankruptcy code — a label lookup,
    not a heuristic, exactly as item 2.02 is for earnings."""

    def _with(self, *extra: Filing):
        return quarterly(NOW - timedelta(days=1500), 12) + list(extra)

    def test_bankruptcy_from_item_1_03(self):
        lc = classify_lifecycle(
            self._with(filing("8-K", NOW - timedelta(days=400), items=("1.03",))),
            as_of=NOW)
        self.assertEqual(lc.reason, DelistingReason.BANKRUPTCY_DISTRESS)

    def test_going_private_from_sc_13e3(self):
        lc = classify_lifecycle(
            self._with(filing("SC 13E3", NOW - timedelta(days=400))), as_of=NOW)
        self.assertEqual(lc.reason, DelistingReason.GOING_PRIVATE)

    def test_merger_needs_both_a_proxy_and_a_delisting_notice(self):
        """Item 2.01 alone is unusable — Sears filed five between 2006 and 2019,
        nearly all ordinary asset sales rather than the company being bought."""
        proxy_only = classify_lifecycle(
            self._with(filing("DEFM14A", NOW - timedelta(days=400))), as_of=NOW)
        self.assertNotEqual(proxy_only.reason, DelistingReason.MERGER_ACQUISITION)

        both = classify_lifecycle(
            self._with(filing("DEFM14A", NOW - timedelta(days=400)),
                       filing("25-NSE", NOW - timedelta(days=390))), as_of=NOW)
        self.assertEqual(both.reason, DelistingReason.MERGER_ACQUISITION)

    def test_exchange_delisting_from_item_3_01(self):
        lc = classify_lifecycle(
            self._with(filing("8-K", NOW - timedelta(days=400), items=("3.01",))),
            as_of=NOW)
        self.assertEqual(lc.reason, DelistingReason.EXCHANGE_DELISTING)

    def test_bankruptcy_outranks_an_earlier_delisting_notice(self):
        """Markers arrive in sequence and the FIRST is not the cause.
        Blockbuster's exchange-delisting notice preceded its bankruptcy by ten
        months; classifying on the earliest marker would call that a listing
        failure rather than an insolvency."""
        lc = classify_lifecycle(
            self._with(filing("8-K", NOW - timedelta(days=700), items=("3.01",)),
                       filing("8-K", NOW - timedelta(days=400), items=("1.03",))),
            as_of=NOW)
        self.assertEqual(lc.reason, DelistingReason.BANKRUPTCY_DISTRESS)

    def test_the_priority_order_is_the_registered_one(self):
        lc = classify_lifecycle(
            self._with(filing("8-K", NOW - timedelta(days=400), items=("1.03", "3.01")),
                       filing("SC 13E3", NOW - timedelta(days=400)),
                       filing("DEFM14A", NOW - timedelta(days=400)),
                       filing("25-NSE", NOW - timedelta(days=400))), as_of=NOW)
        self.assertEqual(lc.reason, DelistingReason.BANKRUPTCY_DISTRESS)


class SilenceTest(unittest.TestCase):
    """A company that stops filing periodic reports has almost certainly gone.
    But silence alone cannot say WHY, and it takes time to become meaningful."""

    def test_long_silence_is_presumed_dead(self):
        lc = classify_lifecycle(quarterly(NOW - timedelta(days=2000), 8), as_of=NOW)
        self.assertEqual(lc.status, LifecycleStatus.PRESUMED_DEAD)

    def test_a_recently_quiet_company_is_still_alive(self):
        """A late filer is not a dead one. The threshold has to clear a missed
        annual report plus grace, or ordinary lateness reads as death."""
        lc = classify_lifecycle(quarterly(NOW - timedelta(days=800), 8), as_of=NOW)
        self.assertEqual(lc.status, LifecycleStatus.ALIVE)

    def test_silence_alone_gives_an_unknown_reason(self):
        lc = classify_lifecycle(quarterly(NOW - timedelta(days=2000), 8), as_of=NOW)
        self.assertEqual(lc.status, LifecycleStatus.PRESUMED_DEAD)
        self.assertEqual(lc.reason, DelistingReason.UNKNOWN)

    def test_an_estate_filing_8ks_forever_does_not_look_alive(self):
        """Lehman Brothers' estate was still filing in 2025, seventeen years
        after the collapse. Liveness is measured on PERIODIC reports only."""
        estate = quarterly(NOW - timedelta(days=2000), 8) + [
            filing("8-K", NOW - timedelta(days=d)) for d in (200, 100, 30)
        ]
        lc = classify_lifecycle(estate, as_of=NOW)
        self.assertEqual(lc.status, LifecycleStatus.PRESUMED_DEAD)


class BankFailureBlindSpotTest(unittest.TestCase):
    """First Republic was seized by the FDIC and sold to JPMorgan. The holding
    company never filed Chapter 11, so item 1.03 never fired — and banks are
    precisely where distress clusters, so the gap is not randomly distributed."""

    def test_a_seized_bank_lands_in_unknown(self):
        lc = classify_lifecycle(quarterly(NOW - timedelta(days=2000), 8), as_of=NOW)
        self.assertEqual(lc.reason, DelistingReason.UNKNOWN)

    def test_unknown_is_treated_as_distress_not_as_neutral(self):
        """Registered in §15. The blind spot removes some of the worst outcomes
        from the industry that produces the worst outcomes, and it does so in
        the flattering direction."""
        for scenario in Scenario:
            self.assertAlmostEqual(
                delisting_return(DelistingReason.UNKNOWN, scenario),
                delisting_return(DelistingReason.BANKRUPTCY_DISTRESS, scenario),
            )


class DelistingReturnTest(unittest.TestCase):
    """The registered three-way sensitivity from §14, with §15's correction."""

    def test_the_three_scenarios_exist(self):
        self.assertEqual({s.value for s in Scenario}, {"neutral", "moderate", "total"})

    def test_total_loss_is_minus_one_for_distress(self):
        self.assertAlmostEqual(
            delisting_return(DelistingReason.BANKRUPTCY_DISTRESS, Scenario.TOTAL), -1.0)

    def test_neutral_is_zero(self):
        self.assertAlmostEqual(
            delisting_return(DelistingReason.BANKRUPTCY_DISTRESS, Scenario.NEUTRAL), 0.0)

    def test_moderate_is_minus_thirty_percent(self):
        self.assertAlmostEqual(
            delisting_return(DelistingReason.BANKRUPTCY_DISTRESS, Scenario.MODERATE), -0.30)

    def test_the_scenario_applies_uniformly_as_registered(self):
        """§14 registered a uniform sweep, and it stays uniform — the reason
        codes make the result INTERPRETABLE, they do not soften the stress
        test. Reason-specific returns would be a fitted parameter."""
        for reason in (DelistingReason.MERGER_ACQUISITION,
                       DelistingReason.GOING_PRIVATE,
                       DelistingReason.BANKRUPTCY_DISTRESS):
            self.assertAlmostEqual(delisting_return(reason, Scenario.TOTAL), -1.0)

    def test_a_live_company_has_no_delisting_return(self):
        self.assertIsNone(delisting_return(DelistingReason.NONE, Scenario.TOTAL))


class CohortReportTest(unittest.TestCase):
    """The counts are the diagnostic. 143 delistings of which 108 are distress
    means terminal-return accuracy is load-bearing; 143 of which 74 are
    acquisitions means a blanket -100% is manufacturing a false negative."""

    def _cohort(self, **counts):
        out = []
        for reason, n in counts.items():
            for _ in range(n):
                out.append(DelistingReason[reason])
        return out

    def test_counts_are_reported(self):
        rep = cohort_report(self._cohort(BANKRUPTCY_DISTRESS=37,
                                         MERGER_ACQUISITION=74,
                                         GOING_PRIVATE=12,
                                         UNKNOWN=20))
        self.assertEqual(rep.total, 143)
        self.assertEqual(rep.counts[DelistingReason.MERGER_ACQUISITION], 74)

    def test_an_acquisition_heavy_cohort_flags_the_total_loss_case_as_absurd(self):
        rep = cohort_report(self._cohort(BANKRUPTCY_DISTRESS=37,
                                         MERGER_ACQUISITION=74,
                                         GOING_PRIVATE=12,
                                         UNKNOWN=20))
        self.assertTrue(rep.total_loss_is_implausible)

    def test_a_distress_heavy_cohort_does_not(self):
        rep = cohort_report(self._cohort(BANKRUPTCY_DISTRESS=108,
                                         MERGER_ACQUISITION=15,
                                         UNKNOWN=20))
        self.assertFalse(rep.total_loss_is_implausible)

    def test_too_many_unknowns_fails_the_gate(self):
        """Registered in §15: above 15% UNKNOWN the result is INSUFFICIENT
        DATA regardless of what the returns say."""
        rep = cohort_report(self._cohort(BANKRUPTCY_DISTRESS=50, UNKNOWN=50))
        self.assertGreater(rep.unknown_share, UNKNOWN_REASON_GATE)
        self.assertFalse(rep.passes_unknown_gate)

    def test_few_unknowns_passes(self):
        rep = cohort_report(self._cohort(BANKRUPTCY_DISTRESS=95, UNKNOWN=5))
        self.assertTrue(rep.passes_unknown_gate)

    def test_an_empty_cohort_does_not_divide_by_zero(self):
        rep = cohort_report([])
        self.assertEqual(rep.total, 0)
        self.assertAlmostEqual(rep.unknown_share, 0.0)


class DeathDatingTest(unittest.TestCase):
    """Both defects below were invisible in fixtures and found by running the
    classifier against companies that really died."""

    def test_death_is_dated_by_the_first_marker_not_the_last(self):
        """Companies file item 1.03 repeatedly through a long bankruptcy — plan
        confirmation, asset sales, emergence. Taking the last one dated Sears'
        death 2019-11-04 against a real Chapter 11 of 2018-10-15, and Bed Bath
        & Beyond 2023-09-29 against 2023-04-24. That error keeps a failing
        company in the universe through the exact months it was failing."""
        first = datetime(2024, 3, 1, tzinfo=UTC)
        history = quarterly(datetime(2020, 1, 1, tzinfo=UTC), 16) + [
            filing("8-K", first, items=("1.03",)),
            filing("8-K", datetime(2024, 9, 1, tzinfo=UTC), items=("1.03",)),
            filing("8-K", datetime(2025, 6, 1, tzinfo=UTC), items=("1.03",)),
        ]
        lc = classify_lifecycle(history, as_of=NOW)
        self.assertEqual(lc.death_date, first)

    def test_a_company_with_no_periodic_filings_still_reaches_the_cohort(self):
        """First Republic found this. Seized by the FDIC, so no Chapter 11 and
        no item 1.03; and no 10-K in the window either, so it reached neither
        DEAD nor PRESUMED_DEAD and cohort_report dropped it for carrying reason
        NONE. A bank failure vanishing from the delisting cohort is exactly the
        survivorship failure this module exists to prevent."""
        only_8ks = [filing("8-K", datetime(2023, 5, 1, tzinfo=UTC)),
                    filing("8-K", datetime(2024, 2, 9, tzinfo=UTC))]
        lc = classify_lifecycle(only_8ks, as_of=NOW)
        self.assertEqual(lc.status, LifecycleStatus.PRESUMED_DEAD)
        self.assertEqual(lc.reason, DelistingReason.UNKNOWN)
        self.assertTrue(lc.reason.is_distress)
        self.assertEqual(cohort_report([lc.reason]).total, 1)

    def test_periodic_reports_still_win_where_they_exist(self):
        """The fallback must not reopen the estate trap: a company WITH
        periodic filings is still judged on those, so seventeen years of estate
        8-Ks do not make Lehman look alive."""
        estate = quarterly(NOW - timedelta(days=2000), 8) + [
            filing("8-K", NOW - timedelta(days=10))
        ]
        self.assertEqual(classify_lifecycle(estate, as_of=NOW).status,
                         LifecycleStatus.PRESUMED_DEAD)


if __name__ == "__main__":
    unittest.main()
