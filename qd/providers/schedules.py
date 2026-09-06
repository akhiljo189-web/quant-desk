"""
qd.providers.schedules — Schedule 13D and 13G beneficial-ownership filings.

The 5%-holder half of the Big Money Score in
`docs/superpowers/specs/2026-09-05-future-leaders-design.md` §5.3. Anyone
acquiring beneficial ownership of more than 5% of a registered class must say
so, and which form they use is itself the signal:

  SCHEDULE 13D   Control intent. An activist, a strategic buyer, someone who
                 means to change what the company does. Short deadline.
  SCHEDULE 13G   The passive box. Index funds, advisers and exempt investors
                 that hold size without intent. Far more common, far less
                 informative per filing.

TWO FORMS THAT SHARE ALMOST NO FIELD NAME

They describe the same fact and their schemas agree on very little. Verified
against real filings on 2026-09-06:

                     13D                          13G
  namespace          .../schedule13D              .../schedule13g   (!)
  event date         dateOfEvent                  eventDateRequiresFilingThisStatement
  issuer CIK         issuerCIK                    issuerCik
  percent            percentOfClass               classPercent
  person block       reportingPersons/            coverPageHeaderReportingPersonDetails
                       reportingPersonInfo          (repeated, flat)
  aggregate held     aggregateAmountOwned         reportingPersonBeneficiallyOwned
                                                    AggregateNumberOfShares
  voting powers      direct children              nested one level deeper
  holder CIK         reportingPersonCIK           absent entirely

The namespaces differ only in the case of the final letter. Anything matching
fully-qualified tags handles one form and returns an empty list for the other —
which does not look like a bug, it looks like a company with no large holders.
Everything here matches on LOCAL element names for that reason, which also
survives the schema version changes that have already happened twice.

THE GROUP DOUBLE-COUNT, which is the trap this module exists for

A fund files jointly with its general partner, its manager and its principal.
All of them report the SAME shares, because all of them beneficially own the
same block. A real filing checked during development listed four reporting
persons against one holding of 1,304,878 shares. Summing `aggregateAmountOwned`
across reporting persons quadruples the position, and the "accumulation" that
results is an artefact of how the filing was structured rather than anything
anyone bought. `collapse_group` exists to prevent it and must be called before
any breadth or size figure is computed.

THE DISCLOSURE LAG IS NOT ONE NUMBER

13D and 13G have different deadlines, and 13G's depends on the filer's class,
which is stated in the rule it cites on the cover page:

  Rule 13d-1(b)   Qualified institutional investor — quarterly, well after
  Rule 13d-1(c)   Passive investor — days
  Rule 13d-1(d)   Exempt investor — quarterly

Reading the class from the filing beats assuming a single lag across all
history. The deadlines themselves were also shortened by the 2023 amendments,
so a fixed rule would be wrong on one side of that date whichever value it took;
what is recorded here is the maximum lag by class, used to bound how stale a
record may be rather than to date it.

WHAT THIS MODULE CANNOT DO

Filings before the structured-XML mandate are HTML or plain text with no
`primary_doc.xml`, and there is no reliable way to read a percentage out of two
decades of free-form cover pages. Those return an empty list. That is correct
behaviour and it is also dangerous, because "no rows" is indistinguishable from
"no large holders" — so `has_structured_data` exists to let a caller tell the
difference, and any archive built from these must record which era it covers.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Iterable, Optional, Sequence

from qd.types import UTC, ensure_utc

logger = logging.getLogger(__name__)

# The reporting threshold. Below it there is no obligation to file at all, so a
# reported stake under 5% means a holder on the way OUT, not a small position.
THRESHOLD_PCT = 5.0


class ScheduleKind(str, Enum):
    D = "13D"
    G = "13G"


class FilerClass(str, Enum):
    """Who is filing, which sets both the meaning and the deadline."""
    ACTIVIST = "activist"        # any 13D — control intent by definition
    QII = "institutional"        # Rule 13d-1(b)
    PASSIVE = "passive"          # Rule 13d-1(c)
    EXEMPT = "exempt"            # Rule 13d-1(d)
    UNKNOWN = "unknown"


# Maximum staleness by class, in days. Used to bound how old the underlying
# position may be — NOT to date it, which `known_at` does exactly.
MAX_LAG_DAYS = {
    FilerClass.ACTIVIST: 10,
    FilerClass.PASSIVE: 10,
    FilerClass.QII: 45,
    FilerClass.EXEMPT: 45,
    FilerClass.UNKNOWN: 45,      # assume the slower case
}

_RULE_CLASS = (
    (re.compile(r"13d-1\s*\(\s*b\s*\)", re.I), FilerClass.QII),
    (re.compile(r"13d-1\s*\(\s*c\s*\)", re.I), FilerClass.PASSIVE),
    (re.compile(r"13d-1\s*\(\s*d\s*\)", re.I), FilerClass.EXEMPT),
)


@dataclass(frozen=True, slots=True)
class BeneficialOwnership:
    """One reporting person on one Schedule 13D/G filing.

    `event_time` is the date that triggered the obligation — the day the stake
    crossed the threshold or materially changed. `known_at` is the filing's
    acceptance instant. The gap between them is days for an activist and can be
    a quarter and a half for an institution, which is why the two are never
    conflated and why `filer_class` is carried on every record.
    """
    kind: ScheduleKind
    issuer_cik: str
    issuer_name: str
    cusip: str
    holder_name: str
    holder_cik: str
    event_date: datetime
    accepted_at: datetime
    percent_of_class: Optional[float]
    shares: Optional[float]
    sole_voting: Optional[float] = None
    shared_voting: Optional[float] = None
    sole_dispositive: Optional[float] = None
    shared_dispositive: Optional[float] = None
    person_type: str = ""
    filer_class: FilerClass = FilerClass.UNKNOWN
    is_amendment: bool = False
    accession: str = ""
    security_title: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_date", ensure_utc(self.event_date))
        object.__setattr__(self, "accepted_at", ensure_utc(self.accepted_at))

    @property
    def event_time(self) -> datetime:
        return self.event_date

    @property
    def known_at(self) -> datetime:
        return self.accepted_at

    @property
    def is_activist(self) -> bool:
        """13D means the holder has declared control intent.

        The single most informative bit on the filing, and the reason the two
        forms must never be pooled: a 13G from an index fund tracks the index,
        while a 13D is a person saying they intend to change the company.
        """
        return self.kind is ScheduleKind.D

    @property
    def is_exit(self) -> bool:
        """An amendment reporting a stake at or below the threshold.

        A holder leaving is the opposite signal to a holder arriving, and it
        arrives on an identically-shaped filing. Storing only the percentage
        would let an exit read as an ordinary disclosure.
        """
        return (
            self.is_amendment
            and self.percent_of_class is not None
            and self.percent_of_class < THRESHOLD_PCT
        )

    @property
    def crosses_threshold(self) -> bool:
        return self.percent_of_class is not None and self.percent_of_class >= THRESHOLD_PCT

    @property
    def max_disclosure_lag_days(self) -> int:
        return MAX_LAG_DAYS[self.filer_class]


def has_structured_data(document: str) -> bool:
    """Whether this document is a machine-readable filing at all.

    Callers need this to tell "no large holders" from "this era predates the
    structured mandate", which are the same empty list otherwise.
    """
    head = document.lstrip()[:2000]
    return "edgarSubmission" in head and "<" in head


def parse_schedule_13(
    xml: str,
    *,
    accepted_at: datetime,
    accession: str = "",
) -> list[BeneficialOwnership]:
    """Parse one Schedule 13D or 13G into one record per reporting person.

    `accepted_at` comes from the filing index, never from the document — the
    document knows when the stake changed and nothing about when it was
    published. It is required rather than defaulted so it cannot be forgotten
    into a value that leaks.

    Unparseable or unstructured documents return an empty list rather than
    raising: one bad filing in a bulk pull must not end the run.
    """
    if not has_structured_data(xml):
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.debug("13D/G: unparseable document (%s): %s", accession or "?", exc)
        return []

    accepted = ensure_utc(accepted_at)

    submission = (_text(root, "submissionType") or "").strip().upper()
    kind = ScheduleKind.D if "13D" in submission else ScheduleKind.G
    is_amendment = submission.endswith("/A")

    issuer_cik = _pad_cik(_text(root, "issuerCIK") or _text(root, "issuerCik"))
    issuer_name = (_text(root, "issuerName") or "").strip()
    cusip = (_text(root, "issuerCusipNumber") or "").strip()
    title = (_text(root, "securitiesClassTitle") or "").strip()

    event = _parse_us_date(
        _text(root, "dateOfEvent")
        or _text(root, "eventDateRequiresFilingThisStatement")
    )
    if event is None:
        logger.debug("13D/G: no readable event date (%s)", accession or "?")
        return []

    # A stake cannot change after the filing that reports it. Such a row would
    # have an event_time later than its known_at — a record knowable before it
    # happened, the one shape the point-in-time layer forbids.
    if event > accepted:
        logger.debug("13D/G: dropping %s, event %s after acceptance %s",
                     issuer_name or "?", event.date(), accepted.date())
        return []

    filer_class = FilerClass.ACTIVIST if kind is ScheduleKind.D else _filer_class(root)

    # 13D nests each person under reportingPersonInfo; 13G repeats a flat
    # coverPageHeaderReportingPersonDetails block. Collect whichever exists.
    people = _find_all(root, "reportingPersonInfo") or \
        _find_all(root, "coverPageHeaderReportingPersonDetails")

    out: list[BeneficialOwnership] = []
    for p in people:
        name = (_text(p, "reportingPersonName") or "").strip()
        if not name:
            continue
        out.append(BeneficialOwnership(
            kind=kind,
            issuer_cik=issuer_cik,
            issuer_name=issuer_name,
            cusip=cusip,
            holder_name=name,
            holder_cik=_pad_cik(_text(p, "reportingPersonCIK")),
            event_date=event,
            accepted_at=accepted,
            # percentOfClass on 13D, classPercent on 13G. None, never 0.0 —
            # a zero would read as a holder who has exited.
            percent_of_class=_parse_float(
                _text(p, "percentOfClass") or _text(p, "classPercent")),
            shares=_parse_float(
                _text(p, "aggregateAmountOwned")
                or _text(p, "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")),
            sole_voting=_parse_float(_text(p, "soleVotingPower")),
            shared_voting=_parse_float(_text(p, "sharedVotingPower")),
            sole_dispositive=_parse_float(_text(p, "soleDispositivePower")),
            shared_dispositive=_parse_float(_text(p, "sharedDispositivePower")),
            person_type=(_text(p, "typeOfReportingPerson") or "").strip(),
            filer_class=filer_class,
            is_amendment=is_amendment,
            accession=accession,
            security_title=title,
        ))
    return out


def collapse_group(
    records: Iterable[BeneficialOwnership],
) -> list[BeneficialOwnership]:
    """Collapse joint filers reporting the same block into one position.

    MUST be called before any breadth or size figure is computed. A fund, its
    general partner, its manager and its principal all report the same shares
    on the same filing; four rows describe one position, and counting them as
    four independent holders is how a "multiple institutions accumulating"
    signal manufactures itself out of a filing convention.

    The group key is the accession, because a joint filing is by definition one
    submission. Two unrelated funds filing separately keep their own rows even
    when they hold identical amounts — those are genuinely independent, which
    is exactly the breadth the design's Big Money Score is trying to measure.

    Within a group the LARGEST reported stake is kept. Members sometimes report
    slightly different totals, and taking the largest avoids understating a
    position that is genuinely held.
    """
    groups: dict[tuple[str, str], list[BeneficialOwnership]] = {}
    for r in records:
        groups.setdefault((r.accession, r.issuer_cik), []).append(r)

    out: list[BeneficialOwnership] = []
    for members in groups.values():
        out.append(max(members, key=lambda r: (
            r.shares if r.shares is not None else -1.0,
            r.percent_of_class if r.percent_of_class is not None else -1.0,
        )))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — all namespace-agnostic, matching on local element names
# ─────────────────────────────────────────────────────────────────────────────

def _local(tag: str) -> str:
    """Strip any `{namespace}` prefix from an element tag."""
    return tag.rsplit("}", 1)[-1]


def _find_all(node: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in node.iter() if _local(e.tag) == name]


def _text(node: ET.Element, name: str) -> Optional[str]:
    """First descendant with this local name that carries text.

    Local-name matching rather than a qualified path: 13D and 13G declare
    namespaces differing only in the case of one letter, and both have already
    changed schema version. A qualified lookup would be a silent outage on
    whichever form it was not written against.
    """
    for e in node.iter():
        if _local(e.tag) == name and e.text and e.text.strip():
            return e.text
    return None


def _parse_us_date(raw: Optional[str]) -> Optional[datetime]:
    """MM/DD/YYYY, as 13D/G writes it — unlike Form 4's ISO dates.

    Read as ISO these either fail outright or, on an ambiguous value like
    03/04/2026, silently transpose month and day. Kept at UTC midnight with no
    timezone conversion, so a date never slips into the previous evening.
    """
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", "").replace("%", "").replace("$", "")
    try:
        return float(cleaned)
    except ValueError:
        return None


def _pad_cik(raw: Optional[str]) -> str:
    if not raw:
        return ""
    digits = raw.strip()
    return f"{int(digits):010d}" if digits.isdigit() else digits


def _filer_class(root: ET.Element) -> FilerClass:
    """Read the filer's class from the rule cited on the cover page."""
    cited = " ".join(
        e.text for e in root.iter()
        if _local(e.tag).startswith("designateRule") and e.text
    )
    for pattern, klass in _RULE_CLASS:
        if pattern.search(cited):
            return klass
    return FilerClass.UNKNOWN


__all__ = [
    "BeneficialOwnership",
    "ScheduleKind",
    "FilerClass",
    "parse_schedule_13",
    "collapse_group",
    "has_structured_data",
    "THRESHOLD_PCT",
    "MAX_LAG_DAYS",
]
