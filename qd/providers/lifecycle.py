"""
qd.providers.lifecycle — did this company die, when, and why.

The survivorship machinery for the future-leaders design (§3, §15, §17). It
exists because of one sentence in §3: a universe missing its failures turns
"insider buying predicts returns" into "insider buying at companies that
survived predicts returns", which is true and worthless.

WHY THIS IS FREE

Vendors delete failed companies; the SEC never does. Anything that ever filed
is in EDGAR permanently, so the survivorship-free universe costs nothing —
which is what removed the CRSP purchase from the critical path (§14). What
EDGAR does not give is the terminal return, and that gap is handled by the
registered three-way sensitivity below rather than by buying data.

DEATH IS A LABEL, NOT A GUESS

`providers/edgar.py` reads earnings announcements from 8-K item 2.02 because
the filer applies that code under legal obligation. The same property holds for
death, and it is the reason this module can classify rather than infer:

  8-K item 1.03   Bankruptcy or receivership. Verified against real filings —
                  Sears 2018-10-15, Bed Bath & Beyond 2023-04-24, Silicon
                  Valley Bank 2023-03-10, Blockbuster 2010-09-24, each matching
                  the real Chapter 11 date to the day.
  SC 13E3         Going-private transaction. Purpose-built form.
  DEFM14A + 25    Merger proxy plus a delisting notification. BOTH are needed.
  8-K item 3.01   Failure to satisfy a listing standard.

  8-K item 2.01   NOT USABLE ALONE. "Completion of acquisition or disposition
                  of assets" covers ordinary asset sales; Sears filed five
                  between 2006 and 2019 while very much still Sears.

FOUR TRAPS, each found by testing against companies known to have died:

  THE FIRST MARKER IS NOT THE CAUSE   Blockbuster's exchange-delisting notice
                  preceded its bankruptcy by ten months. Classifying on the
                  earliest marker calls an insolvency a listing failure.
                  Resolution is by PRIORITY over the terminal cluster.

  ESTATES KEEP FILING   Lehman Brothers' estate was still filing in 2025,
                  seventeen years after the collapse. Liveness is therefore
                  measured on PERIODIC reports only — 8-K traffic proves
                  nothing about whether a company still trades.

  BANK FAILURES ARE INVISIBLE   First Republic was seized by the FDIC and sold
                  to JPMorgan. The holding company never filed Chapter 11, so
                  item 1.03 never fired and it lands in UNKNOWN. Banks are
                  precisely where distress clusters, so this blind spot is not
                  randomly distributed — it removes some of the worst outcomes
                  from the industry that produces the worst outcomes, in the
                  flattering direction. UNKNOWN therefore takes the DISTRESS
                  treatment, and above 15% of a cohort it fails the gate.

  HINDSIGHT        A company that died in 2020 was alive in 2015. Every
                  classification is as-of a date and sees only filings accepted
                  by then. Marking it dead earlier would delete from the
                  universe exactly the companies that went on to fail — the
                  survivorship bias inverted, and no less fatal.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Iterable, Optional, Sequence

from qd.providers.edgar import Filing
from qd.types import ensure_utc

logger = logging.getLogger(__name__)

# How long a company must go without a PERIODIC report before it is presumed
# gone. A 10-K is annual, so this has to clear one missed annual filing plus
# grace, or ordinary lateness reads as death. 450 days is deliberately
# generous: a false "dead" removes a live company from the universe, while a
# false "alive" merely keeps a name that will stop having prices anyway.
SILENCE_DAYS = 450

# Above this share of UNKNOWN delisting reasons the cohort cannot be
# interpreted, and the result is INSUFFICIENT DATA whatever the returns say.
# Registered in §15.
UNKNOWN_REASON_GATE = 0.15

PERIODIC_FORMS = ("10-K", "10-K405", "10-KSB", "10-Q", "10-QSB", "20-F", "40-F")
BANKRUPTCY_ITEM = "1.03"
LISTING_ITEM = "3.01"
DELISTING_FORMS = ("25", "25-NSE")
DEREGISTRATION_FORMS = ("15-12B", "15-12G", "15F-12B", "15F-12G")
GOING_PRIVATE_FORMS = ("SC 13E3", "SC 13E-3")
MERGER_PROXY_FORMS = ("DEFM14A", "PREM14A")


class LifecycleStatus(str, Enum):
    ALIVE = "alive"
    DEAD = "dead"                    # a legal marker says so
    PRESUMED_DEAD = "presumed_dead"  # silence only
    UNKNOWN = "unknown"              # nothing to go on


class DelistingReason(str, Enum):
    NONE = "none"                            # still listed
    BANKRUPTCY_DISTRESS = "bankruptcy_distress"
    MERGER_ACQUISITION = "merger_acquisition"
    GOING_PRIVATE = "going_private"
    EXCHANGE_DELISTING = "exchange_delisting"
    UNKNOWN = "unknown"

    @property
    def is_distress(self) -> bool:
        """UNKNOWN counts as distress — see the bank-failure trap above."""
        return self in (DelistingReason.BANKRUPTCY_DISTRESS, DelistingReason.UNKNOWN)


# Priority for resolving the terminal marker cluster. Registered in §15.
_REASON_PRIORITY = (
    DelistingReason.BANKRUPTCY_DISTRESS,
    DelistingReason.GOING_PRIVATE,
    DelistingReason.MERGER_ACQUISITION,
    DelistingReason.EXCHANGE_DELISTING,
    DelistingReason.UNKNOWN,
)


class Scenario(str, Enum):
    """The registered delisting-return sweep from §14."""
    NEUTRAL = "neutral"      # 0% — the position simply stops
    MODERATE = "moderate"    # -30% — the conventional missing-delisting figure
    TOTAL = "total"          # -100% — every delisted holding went to zero


_SCENARIO_RETURN = {
    Scenario.NEUTRAL: 0.0,
    Scenario.MODERATE: -0.30,
    Scenario.TOTAL: -1.0,
}


@dataclass(frozen=True, slots=True)
class CompanyLifecycle:
    """Whether a company was alive on a given date, and if not, why not."""
    cik: str
    as_of: datetime
    status: LifecycleStatus
    reason: DelistingReason
    death_date: Optional[datetime] = None
    last_periodic_at: Optional[datetime] = None
    markers: tuple[str, ...] = ()

    @property
    def is_investable(self) -> bool:
        return self.status is LifecycleStatus.ALIVE

    @property
    def reason_is_certain(self) -> bool:
        """Whether a legal marker named the cause, rather than silence."""
        return self.status is LifecycleStatus.DEAD


@dataclass(frozen=True, slots=True)
class CohortReport:
    """The composition of a set of delistings — the diagnostic §15 registered.

    143 delistings of which 108 are distress means terminal-return accuracy is
    load-bearing and the paid data may be needed. 143 of which 74 are
    acquisitions means a blanket -100% is absurd, and would be manufacturing a
    false negative rather than a conservative one.
    """
    counts: dict[DelistingReason, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def unknown_share(self) -> float:
        if not self.total:
            return 0.0
        return self.counts.get(DelistingReason.UNKNOWN, 0) / self.total

    @property
    def passes_unknown_gate(self) -> bool:
        """Registered in §15: above 15% UNKNOWN the result is INSUFFICIENT
        DATA regardless of what the returns say."""
        return self.unknown_share <= UNKNOWN_REASON_GATE

    @property
    def distress_share(self) -> float:
        if not self.total:
            return 0.0
        return sum(n for r, n in self.counts.items() if r.is_distress) / self.total

    @property
    def total_loss_is_implausible(self) -> bool:
        """Whether the -100% scenario is materially harsher than reality.

        True when most exits were not distress — acquisitions and going-private
        deals typically pay a premium, so marking them as total losses converts
        wins into wipeouts. When this is true, a strategy that FAILS the TOTAL
        scenario has not necessarily failed; read the NEUTRAL and MODERATE runs
        alongside it and say so.
        """
        return self.total > 0 and self.distress_share < 0.5


def classify_lifecycle(
    filings: Sequence[Filing],
    *,
    as_of: datetime,
    silence_days: int = SILENCE_DAYS,
) -> CompanyLifecycle:
    """Was this company alive on `as_of`, and if not, why not.

    Only filings accepted by `as_of` are visible. A 2020 bankruptcy cannot make
    a company dead in 2017 — doing so would delete from the universe precisely
    the names that went on to fail, which is survivorship bias inverted.
    """
    now = ensure_utc(as_of)
    visible = [f for f in filings if ensure_utc(f.accepted_at) <= now]
    cik = filings[0].cik if filings else ""

    if not visible:
        return CompanyLifecycle(cik=cik, as_of=now, status=LifecycleStatus.UNKNOWN,
                                reason=DelistingReason.NONE)

    # Liveness is measured on PERIODIC reports only. An estate filing 8-Ks for
    # seventeen years is not a listed company.
    periodic = [f for f in visible if f.form in PERIODIC_FORMS]
    last_periodic = max((ensure_utc(f.accepted_at) for f in periodic), default=None)

    reason, death, markers = _terminal_reason(visible)

    if reason is not DelistingReason.UNKNOWN:
        return CompanyLifecycle(
            cik=cik, as_of=now, status=LifecycleStatus.DEAD, reason=reason,
            death_date=death, last_periodic_at=last_periodic, markers=markers,
        )

    # A company with no periodic reports at all still has to be assessed, or it
    # leaves the cohort silently. First Republic is the case that found this:
    # seized by the FDIC, no Chapter 11, and no 10-K in the window either, so
    # it reached neither DEAD nor PRESUMED_DEAD and `cohort_report` dropped it
    # for carrying reason NONE. A bank failure vanishing from the delisting
    # cohort is precisely the survivorship failure this module exists to stop.
    #
    # Periodic reports remain the liveness test where they exist (the estate
    # trap). Only when there are none does any filing serve as the last sign
    # of life.
    last_activity = last_periodic or max(ensure_utc(f.accepted_at) for f in visible)

    if (now - last_activity) > timedelta(days=silence_days):
        # Gone, but silence cannot say why. This is where FDIC-seized banks
        # land, and why UNKNOWN carries the distress treatment.
        return CompanyLifecycle(
            cik=cik, as_of=now, status=LifecycleStatus.PRESUMED_DEAD,
            reason=DelistingReason.UNKNOWN, death_date=last_activity,
            last_periodic_at=last_periodic, markers=markers,
        )

    return CompanyLifecycle(
        cik=cik, as_of=now, status=LifecycleStatus.ALIVE,
        reason=DelistingReason.NONE, last_periodic_at=last_periodic, markers=markers,
    )


def _terminal_reason(
    visible: Sequence[Filing],
) -> tuple[DelistingReason, Optional[datetime], tuple[str, ...]]:
    """Resolve the death markers by priority, not by which came first.

    Blockbuster's exchange-delisting notice preceded its bankruptcy by ten
    months; taking the earliest marker would record an insolvency as a listing
    failure.
    """
    found: dict[DelistingReason, datetime] = {}
    markers: list[str] = []
    has_proxy = has_delisting_notice = False

    for f in visible:
        when = ensure_utc(f.accepted_at)
        items = tuple(f.items or ())

        if f.form.startswith("8-K") and BANKRUPTCY_ITEM in items:
            _keep_earliest(found, DelistingReason.BANKRUPTCY_DISTRESS, when)
            markers.append(f"8-K item {BANKRUPTCY_ITEM} @ {when:%Y-%m-%d}")
        if f.form.startswith("8-K") and LISTING_ITEM in items:
            _keep_earliest(found, DelistingReason.EXCHANGE_DELISTING, when)
            markers.append(f"8-K item {LISTING_ITEM} @ {when:%Y-%m-%d}")
        if f.form in GOING_PRIVATE_FORMS:
            _keep_earliest(found, DelistingReason.GOING_PRIVATE, when)
            markers.append(f"{f.form} @ {when:%Y-%m-%d}")
        if f.form in MERGER_PROXY_FORMS:
            has_proxy = True
            markers.append(f"{f.form} @ {when:%Y-%m-%d}")
        if f.form in DELISTING_FORMS:
            has_delisting_notice = True
            _keep_earliest(found, DelistingReason.EXCHANGE_DELISTING, when)
            markers.append(f"{f.form} @ {when:%Y-%m-%d}")

    # A merger proxy alone means a vote was proposed, not that the company
    # left the market; a delisting notice alone can follow any cause. Only
    # both together identify an acquisition.
    if has_proxy and has_delisting_notice:
        _keep_earliest(found, DelistingReason.MERGER_ACQUISITION,
                     found.get(DelistingReason.EXCHANGE_DELISTING, visible[-1].accepted_at))

    for reason in _REASON_PRIORITY:
        if reason in found:
            return reason, found[reason], tuple(markers)
    return DelistingReason.UNKNOWN, None, tuple(markers)


def _keep_earliest(store: dict, key: DelistingReason, when: datetime) -> None:
    """Keep the FIRST occurrence of each marker.

    Companies file 8-K item 1.03 repeatedly through a long bankruptcy — plan
    confirmation, asset sales, emergence — so the last one can be years after
    the collapse. Measured: taking the latest dated Sears' death 2019-11-04
    against a real Chapter 11 of 2018-10-15, and Bed Bath & Beyond 2023-09-29
    against 2023-04-24. That error keeps a failing company in the universe
    through the exact months it was failing, which flatters every result
    computed over it.

    The first marker is when the company entered distress, which is what a
    universe needs to know.
    """
    if key not in store or when < store[key]:
        store[key] = when


def delisting_return(reason: DelistingReason, scenario: Scenario) -> Optional[float]:
    """The assumed return on a position in a company that left the universe.

    The scenario applies UNIFORMLY across reasons, exactly as registered in
    §14. That is deliberate: reason-specific returns would be a fitted
    parameter chosen without evidence, and the sweep is a stress test rather
    than an estimate. What the reason codes buy is INTERPRETATION — see
    `CohortReport.total_loss_is_implausible`, which says whether the -100%
    corner is harsher than reality for this particular cohort.

    Returns None for a company that never left.
    """
    if reason is DelistingReason.NONE:
        return None
    return _SCENARIO_RETURN[scenario]


def cohort_report(reasons: Iterable[DelistingReason]) -> CohortReport:
    """Count delistings by reason. Reported alongside every result."""
    counts = Counter(r for r in reasons if r is not DelistingReason.NONE)
    return CohortReport(counts=dict(counts))


__all__ = [
    "CompanyLifecycle",
    "CohortReport",
    "LifecycleStatus",
    "DelistingReason",
    "Scenario",
    "classify_lifecycle",
    "cohort_report",
    "delisting_return",
    "SILENCE_DAYS",
    "UNKNOWN_REASON_GATE",
]
