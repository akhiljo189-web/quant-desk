"""
qd.providers.holdings13f — Form 13F institutional holdings.

The institutional-breadth component of the Big Money Score in
`docs/superpowers/specs/2026-09-05-future-leaders-design.md` §5.3, and
deliberately the LOWEST-weighted one. The design registers it at 10% and calls
it confirmation only, never a trigger, for a reason worth restating at the top
of the module that produces it:

    A 13F IS A SNAPSHOT, NOT A TRANSACTION RECORD.

It reports what a manager held on the last day of a quarter, and it is due 45
days after that day. A position appearing in a filing published on 14 February
may have been opened on 2 October, and closed on 5 January — six weeks before
anyone could read about it. The design's falsification test 4 says exactly
this: if 13F breadth scores as the strongest factor, suspect a leak in the
quarter-end-to-filing-date join before believing it.

It also omits short positions, most derivatives, and non-US holdings, so
"institutional ownership rose" is a partial view by construction.

THREE ERA TRAPS, each measured against live EDGAR rather than assumed, and each
producing entirely plausible numbers when got wrong:

  VALUE UNITS      The `value` field was in THOUSANDS of dollars before 2023
                   and in WHOLE DOLLARS after. Measured on real filings: the
                   median implied price per share (value / shares) was $0.027
                   in 2021 Q3 and $228.53 in 2023 Q3. A parser with one
                   convention is wrong by 1000x on one side of that line, and
                   the resulting step change in "institutional position size"
                   sits exactly on a date — the same class of time-dependent
                   artefact as the 13D/G format boundary in GATE
                   DG-HISTORY-001. Everything here is normalised to dollars,
                   with the raw figure kept beside it.

  AMENDMENT TYPE   `RESTATEMENT` replaces the prior table wholesale;
                   `NEW HOLDINGS` adds to it. Both were observed from the same
                   filer in one quarter. Treating them alike either
                   double-counts a quarter's positions or silently drops them,
                   and an amendment carrying no type at all is UNKNOWN rather
                   than assumed, because guessing wrong in either direction
                   corrupts the quarter.

  13F-NT           A notice: "my holdings are reported by another filer". It
                   carries no information table. That is not a manager who
                   owns nothing, and scoring it as zero would understate
                   ownership for every manager who reports through a parent.

SH VERSUS PRN

`sshPrnamtType` is SH for a share count and PRN for a principal amount — a face
value in dollars, on a note. They are different units in the same field.
Summing them adds bond principal to equity share counts and produces positions
that look enormous. Only SH rows are equity positions.

DATE FORMATS, for the record: Form 4 writes YYYY-MM-DD, Schedule 13D/G writes
MM/DD/YYYY, and 13F writes MM-DD-YYYY. Three SEC forms, three formats, all in
this one project.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Iterable, Optional, Sequence

from qd.types import UTC, ensure_utc

logger = logging.getLogger(__name__)

# Form 13F was amended to report values in whole dollars rather than thousands.
# Taken from the regulation rather than sniffed from the data: a heuristic on
# implied price would misfire on a portfolio of penny stocks or of Berkshire A
# shares, and would do so silently.
VALUE_IN_WHOLE_DOLLARS_FROM = date(2023, 1, 1)

# A 13F is due 45 days after quarter end, so a holding can be four and a half
# months stale before it is readable. Used to bound staleness, never to date
# anything — `known_at` does the dating.
MAX_DISCLOSURE_LAG_DAYS = 45


class ReportType(str, Enum):
    HOLDINGS = "13f_holdings"           # 13F-HR — has a table
    NOTICE = "13f_notice"               # 13F-NT — reported by someone else
    COMBINATION = "13f_combination"     # some held here, some elsewhere
    UNKNOWN = "unknown"


class AmendmentType(str, Enum):
    NONE = "none"                       # not an amendment
    RESTATEMENT = "restatement"         # replaces the prior table wholesale
    NEW_HOLDINGS = "new_holdings"       # adds to it
    UNKNOWN = "unknown"                 # an amendment that did not say which


class ShareType(str, Enum):
    SH = "SH"                           # share count
    PRN = "PRN"                         # principal amount, in dollars
    UNKNOWN = "unknown"


class HoldingsState(str, Enum):
    """Why a filing produced the holdings it did — including none.

    The same distinction `CoverageState` draws in `providers/schedules.py`, for
    the same reason: an empty result must never be silently scoreable as "this
    manager held nothing".
    """
    PARSED = "parsed"
    REPORTED_ELSEWHERE = "reported_elsewhere"    # 13F-NT — not zero
    PARSE_FAILED = "parse_failed"
    NO_HOLDINGS = "no_holdings"                  # read fine, genuinely empty

    @property
    def is_evidence_of_absence(self) -> bool:
        return self is HoldingsState.NO_HOLDINGS


@dataclass(frozen=True, slots=True)
class Filing13FCover:
    """The cover page: who filed, for which quarter, and what kind of report."""
    manager_name: str
    manager_cik: str
    period_of_report: datetime
    accepted_at: datetime
    report_type: ReportType
    amendment_type: AmendmentType
    is_amendment: bool
    state: HoldingsState
    other_managers_count: int = 0
    file_number: str = ""
    accession: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "period_of_report", ensure_utc(self.period_of_report))
        object.__setattr__(self, "accepted_at", ensure_utc(self.accepted_at))

    @property
    def event_time(self) -> datetime:
        """Quarter end — the date the holdings were true."""
        return self.period_of_report

    @property
    def known_at(self) -> datetime:
        return self.accepted_at

    @property
    def disclosure_lag_days(self) -> int:
        return (self.accepted_at - self.period_of_report).days

    @property
    def replaces_prior(self) -> bool:
        """Whether this filing supersedes the manager's earlier table.

        Only a declared RESTATEMENT does. An amendment of unknown type does
        NOT, because assuming replacement drops positions the manager still
        holds — the more damaging of the two possible errors, since a dropped
        holding reads as a sale that never happened.
        """
        return self.amendment_type is AmendmentType.RESTATEMENT


@dataclass(frozen=True, slots=True)
class Holding:
    """One line of one information table, in normalised units."""
    cusip: str
    issuer_name: str
    title_of_class: str
    value_raw: float                    # exactly as filed
    value_usd: float                    # normalised to dollars for the era
    shares: float
    share_type: ShareType
    accepted_at: datetime
    period_of_report: Optional[datetime] = None
    investment_discretion: str = ""
    sole_voting: Optional[float] = None
    shared_voting: Optional[float] = None
    no_voting: Optional[float] = None
    other_manager: str = ""
    accession: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "accepted_at", ensure_utc(self.accepted_at))
        if self.period_of_report is not None:
            object.__setattr__(self, "period_of_report",
                               ensure_utc(self.period_of_report))

    @property
    def known_at(self) -> datetime:
        return self.accepted_at

    @property
    def is_equity_position(self) -> bool:
        """SH rows only. A PRN row is bond principal in the same field."""
        return self.share_type is ShareType.SH and self.shares > 0

    @property
    def implied_price(self) -> float:
        """value / shares — the check that catches a units error.

        A plausible share price means the era normalisation worked. A figure a
        thousand times too small or large means it did not, and this is the one
        number that makes that visible rather than merely wrong.
        """
        return self.value_usd / self.shares if self.shares else 0.0


def parse_13f_cover(
    xml: str,
    *,
    accepted_at: datetime,
    accession: str = "",
) -> Filing13FCover:
    """Parse a 13F primary document (the cover page).

    Always returns a cover, even on failure, carrying a `state` that says what
    happened — so a caller cannot mistake an unreadable filing for a manager
    with no holdings.
    """
    accepted = ensure_utc(accepted_at)
    blank = Filing13FCover(
        manager_name="", manager_cik="", period_of_report=accepted,
        accepted_at=accepted, report_type=ReportType.UNKNOWN,
        amendment_type=AmendmentType.NONE, is_amendment=False,
        state=HoldingsState.PARSE_FAILED, accession=accession,
    )
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.debug("13F: unparseable cover (%s): %s", accession or "?", exc)
        return blank

    period = _parse_13f_date(
        _text(root, "periodOfReport") or _text(root, "reportCalendarOrQuarter"))
    if period is None:
        logger.debug("13F: no readable period (%s)", accession or "?")
        return blank

    raw_type = (_text(root, "reportType") or "").upper()
    if "NOTICE" in raw_type:
        report_type = ReportType.NOTICE
    elif "COMBINATION" in raw_type:
        report_type = ReportType.COMBINATION
    elif "HOLDINGS" in raw_type:
        report_type = ReportType.HOLDINGS
    else:
        report_type = ReportType.UNKNOWN

    is_amendment = (_text(root, "isAmendment") or "").strip().lower() in {"true", "1", "y"}
    raw_amend = (_text(root, "amendmentType") or "").strip().upper()
    if not is_amendment:
        amendment_type = AmendmentType.NONE
    elif "RESTATEMENT" in raw_amend:
        amendment_type = AmendmentType.RESTATEMENT
    elif "NEW" in raw_amend:
        amendment_type = AmendmentType.NEW_HOLDINGS
    else:
        amendment_type = AmendmentType.UNKNOWN

    # A notice carries no table by design. That is a different fact from a
    # manager holding nothing, and the two must not share a representation.
    state = (HoldingsState.REPORTED_ELSEWHERE
             if report_type is ReportType.NOTICE else HoldingsState.PARSED)

    return Filing13FCover(
        manager_name=(_text(root, "name") or "").strip(),
        manager_cik=_pad_cik(_text(root, "cik")),
        period_of_report=period,
        accepted_at=accepted,
        report_type=report_type,
        amendment_type=amendment_type,
        is_amendment=is_amendment,
        state=state,
        other_managers_count=int(_parse_float(
            _text(root, "otherIncludedManagersCount")) or 0),
        file_number=(_text(root, "form13FFileNumber") or "").strip(),
        accession=accession,
    )


def parse_13f_table(
    xml: str,
    *,
    accepted_at: datetime,
    accession: str = "",
    period_of_report: Optional[datetime] = None,
) -> list[Holding]:
    """Parse a 13F information table into normalised holdings.

    Values are converted to whole dollars using the filing date and the
    regulatory boundary, and the raw figure is kept beside the normalised one
    so the conversion is auditable rather than assumed.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.debug("13F: unparseable table (%s): %s", accession or "?", exc)
        return []

    accepted = ensure_utc(accepted_at)
    multiplier = value_multiplier(accepted)

    out: list[Holding] = []
    for node in _find_all(root, "infoTable"):
        shares = _parse_float(_text(node, "sshPrnamt"))
        value = _parse_float(_text(node, "value"))
        cusip = (_text(node, "cusip") or "").strip()
        # A row with no share count carries no position. Dropped rather than
        # zero-filled: a zero would read as a manager who sold out.
        if shares is None or value is None or not cusip:
            continue

        raw_type = (_text(node, "sshPrnamtType") or "").strip().upper()
        share_type = (ShareType.SH if raw_type == "SH"
                      else ShareType.PRN if raw_type == "PRN"
                      else ShareType.UNKNOWN)

        va = _first(node, "votingAuthority")
        out.append(Holding(
            cusip=cusip,
            issuer_name=(_text(node, "nameOfIssuer") or "").strip(),
            title_of_class=(_text(node, "titleOfClass") or "").strip(),
            value_raw=value,
            value_usd=value * multiplier,
            shares=shares,
            share_type=share_type,
            accepted_at=accepted,
            period_of_report=period_of_report,
            investment_discretion=(_text(node, "investmentDiscretion") or "").strip(),
            sole_voting=_parse_float(_text(va, "Sole")) if va is not None else None,
            shared_voting=_parse_float(_text(va, "Shared")) if va is not None else None,
            no_voting=_parse_float(_text(va, "None")) if va is not None else None,
            other_manager=(_text(node, "otherManager") or "").strip(),
            accession=accession,
        ))
    return out


def value_multiplier(accepted_at: datetime) -> float:
    """1 for filings reporting whole dollars, 1000 for the thousands era.

    Keyed on the FILING date rather than the reported period, because the
    convention belongs to the document. A late filing for an old quarter uses
    the convention in force when it was filed.
    """
    return 1.0 if ensure_utc(accepted_at).date() >= VALUE_IN_WHOLE_DOLLARS_FROM else 1000.0


def net_new_positions(
    current: Iterable[Holding],
    prior: Optional[Iterable[Holding]],
) -> list[Holding]:
    """Equity positions held this quarter and not the last.

    With no prior quarter the answer is an empty list, never the whole current
    holding set. The first quarter of an archive is not a quarter in which
    every institution bought everything, and treating it that way would put an
    accumulation spike at the start of every backtest — precisely where a
    walk-forward is most likely to mistake it for signal.
    """
    if prior is None:
        return []
    held = {h.cusip for h in prior if h.is_equity_position}
    return [h for h in current if h.is_equity_position and h.cusip not in held]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — namespace-agnostic, matching on local element names
# ─────────────────────────────────────────────────────────────────────────────

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_all(node: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in node.iter() if _localname(e.tag) == name]


def _first(node: ET.Element, name: str) -> Optional[ET.Element]:
    for e in node.iter():
        if _localname(e.tag) == name:
            return e
    return None


def _text(node: Optional[ET.Element], name: str) -> Optional[str]:
    if node is None:
        return None
    for e in node.iter():
        if _localname(e.tag) == name and e.text and e.text.strip():
            return e.text
    return None


def _parse_13f_date(raw: Optional[str]) -> Optional[datetime]:
    """MM-DD-YYYY, as 13F writes it. Kept at UTC midnight, never converted."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in ("%m-%d-%Y", "%Y-%m-%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw[:10], fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


def _parse_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw.strip().replace(",", "").replace("$", ""))
    except ValueError:
        return None


def _pad_cik(raw: Optional[str]) -> str:
    if not raw:
        return ""
    digits = raw.strip()
    return f"{int(digits):010d}" if digits.isdigit() else digits


__all__ = [
    "Filing13FCover",
    "Holding",
    "ReportType",
    "AmendmentType",
    "ShareType",
    "HoldingsState",
    "parse_13f_cover",
    "parse_13f_table",
    "value_multiplier",
    "net_new_positions",
    "VALUE_IN_WHOLE_DOLLARS_FROM",
    "MAX_DISCLOSURE_LAG_DAYS",
]
