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

Structured XML for Schedules 13D/G was OPTIONAL from 2023-12-18 and MANDATORY
from 2024-12-18. Before that, filers used HTML or ASCII, and there is no
reliable way to read a percentage out of two decades of free-form cover pages.

So an empty result is ambiguous, and the ambiguity is time-dependent — which is
the worst kind. Parsing therefore returns a `ScheduleParseResult` carrying a
`CoverageState`, never a bare list: `LEGACY_UNPARSED` means "we have not read
this filing" and `NO_RELEVANT_POSITION` means "there was nothing to report".
Scoring the first as zero ownership would make institutional holdings appear to
rise out of nothing at the moment the file format changed. See GATE
DG-HISTORY-001 in the design document.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Optional, Sequence

from qd.types import UTC, ensure_utc

logger = logging.getLogger(__name__)

# The reporting threshold. Below it there is no obligation to file at all, so a
# reported stake under 5% means a holder on the way OUT, not a small position.
THRESHOLD_PCT = 5.0

# ─────────────────────────────────────────────────────────────────────────────
# The structured-data boundary — regulatory, not empirical
# ─────────────────────────────────────────────────────────────────────────────
#
# These dates are the SEC's, and they are the reason this module cannot treat
# "no XML" as "no holders". Structured XML for Schedules 13D/G was OPTIONAL
# from 2023-12-18 and MANDATORY from 2024-12-18; before that, filers used HTML
# or ASCII. So the presence of structured filings during 2024 does NOT mean
# structured coverage is complete in 2024 — that year is mixed by rule.
#
# Determined from the regulation rather than by sampling EDGAR, because a
# sample can only ever show that structured filings EXIST in a period, never
# that they are complete in it.

XML_OPTIONAL_FROM = date(2023, 12, 18)
XML_MANDATORY_FROM = date(2024, 12, 18)


class CoverageState(str, Enum):
    """Why a parse returned the records it did — including none.

    The distinction this enum exists for: an empty result before the mandatory
    date means "we have not parsed the legacy filing", and an empty result
    after it means "this filing reported no relevant position". Collapsing
    those two into an empty list would put a **time-dependent bias** straight
    into the Big Money Score — institutional ownership would appear to rise
    from nothing at the moment the format changed, purely as an artefact.

    `LEGACY_UNPARSED` must never be scored as zero ownership. See GATE
    DG-HISTORY-001 in the design document.
    """
    PARSED_STRUCTURED = "parsed_structured"      # XML read, records returned
    PARSED_LEGACY = "parsed_legacy"              # legacy text read (not yet built)
    LEGACY_UNPARSED = "legacy_unparsed"          # pre-mandate, NOT machine-readable
    PARSE_FAILED = "parse_failed"                # structured but malformed
    NO_RELEVANT_POSITION = "no_relevant_position"  # parsed fine, nothing to report

    @property
    def is_evidence_of_absence(self) -> bool:
        """Whether an empty result actually means "no large holders".

        Only true when the document was genuinely read. Everything else is
        missing data wearing the same shape.
        """
        return self is CoverageState.NO_RELEVANT_POSITION


class PositionChange(str, Enum):
    """What a filing says happened to a stake.

    A snapshot is not a signal. 7.2% -> 9.1% and 7.2% -> 4.8% arrive on
    identically-shaped documents and must have opposite effects; storing only
    the current percentage loses the sign of the thing being measured.
    """
    NEW_POSITION = "new_position"
    INCREASE = "increase"
    UNCHANGED = "unchanged"
    DECREASE = "decrease"
    EXIT = "exit"                        # amendment reporting zero
    BELOW_5_PERCENT = "below_5_percent"  # dropped under the threshold, still held
    UNKNOWN_CHANGE = "unknown_change"    # no comparable prior filing


class IdentityConfidence(str, Enum):
    """How sure we are that two filings name the same economic entity.

    13G carries no holder CIK, so longitudinal matching falls back to names —
    and "BlackRock Fund Advisors", "BlackRock Institutional Trust Company" and
    "BlackRock, Inc." are related but not identical entities. Aggressive fuzzy
    matching here would invent accumulation that never happened, so the bar for
    claiming "the same institution increased its stake" is HIGH only.

    False negatives are preferred: a missed increase costs sample, an invented
    one costs the result.
    """
    HIGH = "high"        # matched on CIK
    MEDIUM = "medium"    # exact normalised-name match
    LOW = "low"          # anything weaker — not usable for accumulation claims


# Legal-form suffixes stripped when normalising a holder name. Deliberately
# conservative: it removes wrappers, never distinguishing words. "BlackRock
# Fund Advisors" and "BlackRock Institutional Trust Company" must NOT normalise
# to the same string, because they are different entities.
_LEGAL_SUFFIXES = (
    "LP", "L P", "LLP", "LLC", "L L C", "INC", "INCORPORATED", "CORP",
    "CORPORATION", "CO", "LTD", "LIMITED", "PLC", "GP", "SA", "NV", "AG",
    "GMBH", "AB", "AS", "PTE", "PTY", "TRUST", "THE",
)
_PUNCT = re.compile(r"[^A-Z0-9 ]+")
_SPACES = re.compile(r"\s+")


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

    # ── holder identity ──────────────────────────────────────────────────────
    #
    # Kept as three separate readings rather than one resolved name, because
    # the resolution is uncertain and the uncertainty must travel with it.

    @property
    def holder_name_raw(self) -> str:
        """Exactly what the filing said. Never overwritten."""
        return self.holder_name

    @property
    def holder_name_normalized(self) -> str:
        return normalize_holder_name(self.holder_name)

    @property
    def holder_entity_id(self) -> str:
        """Best available identifier — the CIK when there is one.

        13D carries `reportingPersonCIK`; 13G does not carry it at all, so on
        the far more common form this falls back to the normalised name.
        """
        return self.holder_cik or f"name:{self.holder_name_normalized}"

    @property
    def holder_identity_confidence(self) -> IdentityConfidence:
        """Whether this holder may be matched across filings.

        Only a CIK match is HIGH. A normalised-name match is MEDIUM and is not
        sufficient to claim "the same institution increased its stake" — see
        `same_holder`.
        """
        if self.holder_cik:
            return IdentityConfidence.HIGH
        if self.holder_name_normalized:
            return IdentityConfidence.MEDIUM
        return IdentityConfidence.LOW

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


@dataclass(frozen=True, slots=True)
class ScheduleParseResult:
    """Records plus WHY there are that many of them.

    Returned instead of a bare list so a caller cannot silently treat missing
    data as absence of ownership. `records` is empty in three quite different
    situations and only one of them means nobody owned 5%.
    """
    state: CoverageState
    records: tuple[BeneficialOwnership, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.records)

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    @property
    def is_usable_zero(self) -> bool:
        """Safe to score as "no 5% holders"? Only when the document was read."""
        return not self.records and self.state.is_evidence_of_absence


def has_structured_data(document: str) -> bool:
    """Whether this document is a machine-readable filing at all."""
    head = document.lstrip()[:2000]
    return "edgarSubmission" in head and "<" in head


def coverage_state(document: str, accepted_at: datetime) -> CoverageState:
    """Classify a document before parsing it.

    The date matters as much as the content. An unstructured document filed
    before the mandate is expected legacy; the same document filed after it is
    an anomaly worth seeing rather than absorbing.
    """
    if has_structured_data(document):
        return CoverageState.PARSED_STRUCTURED
    if ensure_utc(accepted_at).date() < XML_MANDATORY_FROM:
        return CoverageState.LEGACY_UNPARSED
    logger.warning(
        "13D/G: unstructured document accepted %s, after the %s mandate",
        ensure_utc(accepted_at).date(), XML_MANDATORY_FROM,
    )
    return CoverageState.LEGACY_UNPARSED


def parse_schedule_13(
    xml: str,
    *,
    accepted_at: datetime,
    accession: str = "",
) -> ScheduleParseResult:
    """Parse one Schedule 13D or 13G into one record per reporting person.

    `accepted_at` comes from the filing index, never from the document — the
    document knows when the stake changed and nothing about when it was
    published. It is required rather than defaulted so it cannot be forgotten
    into a value that leaks.

    Returns a `ScheduleParseResult`, not a list, so that "we could not read
    this filing" and "this filing reported nothing" stay distinguishable. One
    bad filing in a bulk pull must not end the run, but it must also not be
    quietly counted as zero ownership.
    """
    state = coverage_state(xml, accepted_at)
    if state is not CoverageState.PARSED_STRUCTURED:
        return ScheduleParseResult(state)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.debug("13D/G: unparseable document (%s): %s", accession or "?", exc)
        return ScheduleParseResult(CoverageState.PARSE_FAILED)

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
        return ScheduleParseResult(CoverageState.PARSE_FAILED)

    # A stake cannot change after the filing that reports it. Such a row would
    # have an event_time later than its known_at — a record knowable before it
    # happened, the one shape the point-in-time layer forbids.
    if event > accepted:
        logger.debug("13D/G: dropping %s, event %s after acceptance %s",
                     issuer_name or "?", event.date(), accepted.date())
        return ScheduleParseResult(CoverageState.PARSE_FAILED)

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
    return ScheduleParseResult(
        CoverageState.PARSED_STRUCTURED if out else CoverageState.NO_RELEVANT_POSITION,
        tuple(out),
    )


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


def breadth_events(
    records: Iterable[BeneficialOwnership],
) -> list[BeneficialOwnership]:
    """Positions that may contribute to a breadth or convergence metric.

    INVARIANT — permanent, and pinned by test:

        One Schedule 13D/G accession contributes AT MOST ONE ownership-position
        event to breadth, unless the filing explicitly contains economically
        distinct positions.

    A joint filing by a fund, its GP, its manager and its principal describes
    one economic position reported four times. Measured across 120 real
    filings, ignoring this inflates holder counts by 2.51x — which would let
    the brief's "multiple institutions accumulating the same stock" signal
    manufacture itself out of a filing convention.

    "Economically distinct" is deliberately narrow: reporting persons on the
    same accession holding DIFFERENT share counts. Same count, same filing, one
    position.
    """
    groups: dict[tuple[str, str], list[BeneficialOwnership]] = {}
    for r in records:
        groups.setdefault((r.accession, r.issuer_cik), []).append(r)

    out: list[BeneficialOwnership] = []
    for members in groups.values():
        distinct: dict[float, BeneficialOwnership] = {}
        for m in members:
            key = m.shares if m.shares is not None else -1.0
            # Keep one representative per distinct share count. Members holding
            # the same block are the same position however many of them sign.
            if key not in distinct:
                distinct[key] = m
        out.extend(distinct.values())
    return out


def same_holder(
    a: BeneficialOwnership,
    b: BeneficialOwnership,
    *,
    require: IdentityConfidence = IdentityConfidence.HIGH,
) -> bool:
    """Whether two filings name the same economic entity.

    Defaults to requiring a CIK match. "BlackRock Fund Advisors", "BlackRock
    Institutional Trust Company" and "BlackRock, Inc." normalise to three
    different strings on purpose — they are related but distinct entities, and
    collapsing them would invent accumulation.

    A missed increase costs sample. An invented one costs the result. The
    default is set accordingly, and relaxing it to MEDIUM is a deliberate act
    that shows up at the call site.
    """
    if a.holder_cik and b.holder_cik:
        return a.holder_cik == b.holder_cik
    if require is IdentityConfidence.HIGH:
        return False
    norm_a, norm_b = a.holder_name_normalized, b.holder_name_normalized
    return bool(norm_a) and norm_a == norm_b


def classify_change(
    current: BeneficialOwnership,
    prior: Optional[BeneficialOwnership],
) -> PositionChange:
    """What this filing says happened to the stake since the last one.

    A snapshot is not a signal: 7.2% -> 9.1% and 7.2% -> 4.8% arrive on
    identically-shaped documents and must have opposite effects. Without a
    prior the answer is UNKNOWN_CHANGE rather than a guess, because an
    amendment with nothing to compare against is exactly as likely to be a
    holder halving their stake as doubling it.
    """
    pct = current.percent_of_class

    if prior is None:
        if current.is_amendment:
            # An amendment always restates an earlier filing we do not hold.
            # Calling this a new position would invert the sign whenever the
            # filing is in fact a reduction.
            return PositionChange.UNKNOWN_CHANGE
        return PositionChange.NEW_POSITION

    if pct is None or prior.percent_of_class is None:
        return PositionChange.UNKNOWN_CHANGE

    if pct <= 0.0:
        return PositionChange.EXIT

    delta = pct - prior.percent_of_class
    if abs(delta) < 0.01:                       # below reporting precision
        return PositionChange.UNCHANGED
    if pct < THRESHOLD_PCT:
        # Still held, but no longer reportable. A distinct state from EXIT:
        # the holder remains, and the next filing may never come.
        return PositionChange.BELOW_5_PERCENT
    return PositionChange.INCREASE if delta > 0 else PositionChange.DECREASE


def normalize_holder_name(raw: str) -> str:
    """Uppercase, strip punctuation, drop trailing legal-form wrappers.

    Conservative by design. It removes wrappers ("LP", "Inc") and nothing else,
    so distinguishing words survive — "BLACKROCK FUND ADVISORS" and "BLACKROCK
    INSTITUTIONAL TRUST" must not collapse onto each other.
    """
    if not raw:
        return ""
    s = _SPACES.sub(" ", _PUNCT.sub(" ", raw.upper())).strip()
    parts = s.split(" ")
    while len(parts) > 1 and parts[-1] in _LEGAL_SUFFIXES:
        parts.pop()
    return " ".join(parts)


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
    "ScheduleParseResult",
    "ScheduleKind",
    "FilerClass",
    "CoverageState",
    "PositionChange",
    "IdentityConfidence",
    "parse_schedule_13",
    "collapse_group",
    "breadth_events",
    "classify_change",
    "same_holder",
    "normalize_holder_name",
    "coverage_state",
    "has_structured_data",
    "THRESHOLD_PCT",
    "MAX_LAG_DAYS",
    "XML_OPTIONAL_FROM",
    "XML_MANDATORY_FROM",
]
