"""
qd.providers.forms — SEC Form 4 insider transactions, point-in-time.

This is the trigger channel for the future-leaders hypothesis
(`docs/superpowers/specs/2026-09-05-future-leaders-design.md`), and it is the
one input in that design with a short statutory lag: an insider must report
within two business days of trading, and EDGAR stamps the exact instant the
report became public. Two days is a long time in a latency race and no time at
all in a signal measured over quarters — which is precisely why the effect is
still there to be measured.

THE CENTRAL PROBLEM, and the reason most of this file is subtraction:

Roughly half a million Form 4s are filed a year, and the overwhelming majority
carry no information about what the filer thinks the stock is worth. They are
grants, option exercises, tax withholding on a vest, gifts, and sales
scheduled months earlier under a trading plan. Every one of them parses
cleanly, reports a real share count, and is tagged ACQUIRED or DISPOSED just
like a real trade.

A parser that takes those rows at face value does not measure insider
conviction. It measures how much stock a company pays its executives, which
tracks company size and share price and predicts nothing. The exclusions below
ARE the module; the XML walking is incidental.

THE FIVE TRAPS, all of which produce plausible numbers:

  THE A/D FLAG        `transactionAcquiredDisposedCode` is "A" for a purchase,
                      a grant, an option exercise and a gift received. It
                      answers "did the share count go up", not "did this person
                      choose to buy". Filtering on it instead of on the
                      transaction code admits the entire compensation stream —
                      and compensation is largest at exactly the companies that
                      have already done well, so the resulting "signal" is a
                      momentum and size factor wearing a disguise.

  RULE 10b5-1         A trade scheduled under a written plan months in advance
                      carries no current view. The SEC only added a structured
                      `aff10b5One` flag in 2023; before that the fact appears
                      only in footnote prose. Reading just the flag applies the
                      exclusion to recent filings and quietly stops applying it
                      to the older half of the sample — the half with the most
                      history, and the half a long-horizon backtest leans on.

  DOUBLE COUNTING     An option exercise appears twice: the option leaves the
                      derivative table, the shares arrive in the non-derivative
                      table. Both rows are real, and neither is a purchase.

  THE TWO TIMESTAMPS  `transactionDate` is when the trade happened;
                      `acceptanceDateTime` on the filing is when anyone outside
                      the company could know. Using the former as `known_at`
                      hands the backtest a two-business-day head start on every
                      insider trade in history. The gap is small and it is
                      exactly the gap the whole effect lives in.

  AMENDMENTS          Form 4/A restates an earlier filing. The correction was
                      not available when the original was filed — the same
                      restatement trap `providers/xbrl.py` documents, arriving
                      through a different door.

WHAT THIS MODULE DELIBERATELY DOES NOT DO: score anything. It returns typed,
dated, attributed transactions and answers "was this an open-market purchase"
per row. Aggregation into a conviction number is `qd/features/insider.py`, so
the exclusion rules can be tested without the scoring and vice versa.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Optional

from qd.types import UTC, ensure_utc

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Transaction codes — SEC Form 345 Table I/II code list
# ─────────────────────────────────────────────────────────────────────────────
#
# Only P is a decision to buy at the prevailing price with the filer's own
# money. Only S is the mirror of it. Everything else is compensation
# machinery, a transfer, or a derivative mechanic.

PURCHASE_CODE = "P"          # open-market or private purchase
SALE_CODE = "S"              # open-market or private sale

# Named individually rather than as "everything else", so that a code the SEC
# adds later reads as unknown and scores nothing, instead of silently joining
# whichever bucket a catch-all put it in.
COMPENSATION_CODES = frozenset({
    "A",   # grant, award or other acquisition from the issuer
    "F",   # shares withheld by the issuer to satisfy tax on a vest
    "M",   # exercise or conversion of a derivative held from the issuer
    "X",   # exercise of an in-the-money or at-the-money derivative
    "D",   # disposition to the issuer
    "I",   # discretionary transaction under an employee plan
    "J",   # other acquisition or disposition (footnoted)
})

TRANSFER_CODES = frozenset({
    "G",   # bona fide gift
    "C",   # conversion of a derivative
    "E",   # expiration of a short derivative position
    "H",   # expiration (or cancellation) of a long derivative position
    "O",   # exercise of an out-of-the-money derivative
})

# Matches "10b5-1", "10b5‑1" (non-breaking hyphen), "Rule 10b5 1", "10B5-1".
_PLAN_PROSE = re.compile(r"10\s*b\s*5[\s‐-―-]*1", re.IGNORECASE)


class Seniority(IntEnum):
    """How much weight the filer's view deserves, highest first.

    Ordered so it can be compared and sorted. A ten-percent owner ranks lowest
    on purpose: a sponsor, index fund or PE holder crossing a threshold is
    trading for portfolio reasons, and its Form 4s are mechanical. Treating
    those as insider conviction is how a score ends up tracking fund flows.
    """
    CEO = 5
    CFO = 4
    OFFICER = 3
    DIRECTOR = 2
    TEN_PERCENT = 1
    OTHER = 0


_CEO_TITLE = re.compile(r"\bC\.?E\.?O\.?\b|chief\s+executive", re.IGNORECASE)
_CFO_TITLE = re.compile(r"\bC\.?F\.?O\.?\b|chief\s+financial", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class InsiderTransaction:
    """One row of one Form 4, carrying both timestamps.

    `event_time` is the trade. `known_at` is the filing's acceptance instant —
    when this system could first have acted. They are two to several days
    apart and the difference is not a rounding detail; it is the whole reason
    the disclosure is tradeable rather than already in the price.
    """
    symbol: str
    issuer_cik: str
    owner_cik: str
    owner_name: str
    transaction_date: datetime            # event_time — when they traded
    accepted_at: datetime                 # known_at — when EDGAR published it
    code: str
    acquired: bool                        # the A/D flag; NOT "was this a buy"
    shares: float
    price: Optional[float] = None
    shares_owned_after: Optional[float] = None
    is_director: bool = False
    is_officer: bool = False
    is_ten_percent_owner: bool = False
    officer_title: str = ""
    is_derivative: bool = False
    planned_10b5_1: bool = False
    is_amendment: bool = False
    accession: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_date", ensure_utc(self.transaction_date))
        object.__setattr__(self, "accepted_at", ensure_utc(self.accepted_at))

    @property
    def event_time(self) -> datetime:
        return self.transaction_date

    @property
    def known_at(self) -> datetime:
        return self.accepted_at

    @property
    def value(self) -> Optional[float]:
        """Dollar value, or None when the price is unknown.

        Never zero-filled. A missing price sorted as 0 would rank as the
        smallest purchase in the universe rather than as the unknown it is,
        and would quietly drag down any size-weighted aggregate.
        """
        if self.price is None:
            return None
        return self.shares * self.price

    @property
    def is_open_market_purchase(self) -> bool:
        """The signal. Everything else in this file exists to keep rows out.

        Requires all four: the purchase code, shares actually acquired, a
        non-zero price (money changed hands), and no trading plan. A P coded at
        zero price is a mis-tagged transfer, not a conviction buy.
        """
        return (
            self.code == PURCHASE_CODE
            and self.acquired
            and not self.is_derivative
            and not self.planned_10b5_1
            and self.price is not None
            and self.price > 0
        )

    @property
    def is_open_market_sale(self) -> bool:
        """The mirror, and a much weaker signal in isolation — insiders sell to
        buy houses, diversify and pay tax bills. Scored negatively and lightly.
        """
        return (
            self.code == SALE_CODE
            and not self.acquired
            and not self.is_derivative
            and not self.planned_10b5_1
            and self.price is not None
            and self.price > 0
        )

    @property
    def exclusion_reason(self) -> Optional[str]:
        """Why this row is not a tradeable signal, or None if it is one.

        Exists so a bulk pull can report its own composition — "of 40,000 rows,
        31,000 were compensation, 6,000 were planned sales, 900 were purchases"
        — rather than reporting a purchase count with no denominator. If that
        breakdown ever comes back looking unlike the known shape of Form 4
        filings, the parser is wrong, and a silent count would not show it.
        """
        if self.is_open_market_purchase or self.is_open_market_sale:
            return None
        if self.is_derivative:
            return "derivative table row"
        if self.planned_10b5_1:
            return "Rule 10b5-1 plan"
        if self.code in COMPENSATION_CODES:
            return "compensation"
        if self.code in TRANSFER_CODES:
            return "transfer"
        if self.code in (PURCHASE_CODE, SALE_CODE):
            return "no price disclosed"
        return f"unrecognised code {self.code!r}"

    @property
    def seniority(self) -> Seniority:
        """Officer roles outrank the board; the board outranks a 5% holder.

        An officer who also sits on the board keeps the officer rank — the
        information advantage comes from running the business, not from
        attending its board meetings.
        """
        if self.is_officer:
            if _CEO_TITLE.search(self.officer_title):
                return Seniority.CEO
            if _CFO_TITLE.search(self.officer_title):
                return Seniority.CFO
            return Seniority.OFFICER
        if self.is_director:
            return Seniority.DIRECTOR
        if self.is_ten_percent_owner:
            return Seniority.TEN_PERCENT
        return Seniority.OTHER


# ─────────────────────────────────────────────────────────────────────────────
# Parsing
# ─────────────────────────────────────────────────────────────────────────────

def parse_form4(
    xml: str,
    *,
    accepted_at: datetime,
    accession: str = "",
    symbol: Optional[str] = None,
) -> list[InsiderTransaction]:
    """Parse one Form 4 ownership document into transactions.

    `accepted_at` comes from the filing index (`providers/edgar.py`), NOT from
    the document — the document knows when the trade happened and has no idea
    when it was published. Passing it in is what keeps `known_at` honest, and
    it is required rather than optional so it cannot be forgotten into a
    default that leaks.

    One row per (transaction × reporting owner): a joint filing names several
    people, and each is a separate attributed signal. They share an
    `accession`, so a caller can still tell two people who filed together from
    two people who each decided to buy.

    A document that fails to parse returns an empty list rather than raising.
    One malformed filing in a bulk pull of thousands must not end the run.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        logger.debug("form4: unparseable document (%s): %s", accession or "?", exc)
        return []

    accepted = ensure_utc(accepted_at)

    doc_type = _text(root, "documentType") or "4"
    is_amendment = doc_type.strip().upper().endswith("/A")

    issuer = root.find("issuer")
    ticker = (symbol or _text(issuer, "issuerTradingSymbol") or "").strip().upper()
    issuer_cik = _pad_cik(_text(issuer, "issuerCik"))

    owners = _parse_owners(root)
    if not owners:
        return []

    # Footnote prose is document-scoped: the 10b5-1 disclosure is written once
    # and referenced by id. Rather than resolving each footnote reference — the
    # references are inconsistently placed across two decades of schema
    # versions — a plan mention anywhere in the document marks the document.
    # That is deliberately over-broad: it drops some genuine discretionary
    # trades on filings that also report a planned one. Over-excluding costs
    # sample; under-excluding puts uninformative rows into the signal, and only
    # one of those two errors flatters the result.
    plan_in_footnotes = _has_plan_prose(root)

    out: list[InsiderTransaction] = []
    for table, derivative in (("nonDerivativeTable", False), ("derivativeTable", True)):
        node = root.find(table)
        if node is None:
            continue
        tag = "nonDerivativeTransaction" if not derivative else "derivativeTransaction"
        for txn in node.findall(tag):
            parsed = _parse_transaction(
                txn,
                ticker=ticker,
                issuer_cik=issuer_cik,
                accepted=accepted,
                accession=accession,
                derivative=derivative,
                is_amendment=is_amendment,
                plan_in_footnotes=plan_in_footnotes,
                owners=owners,
            )
            out.extend(parsed)

    return out


def _parse_transaction(
    txn: ET.Element,
    *,
    ticker: str,
    issuer_cik: str,
    accepted: datetime,
    accession: str,
    derivative: bool,
    is_amendment: bool,
    plan_in_footnotes: bool,
    owners: list[dict],
) -> list[InsiderTransaction]:
    date = _parse_date(_value(txn, "transactionDate"))
    if date is None:
        return []

    # A trade cannot post-date the filing that reports it. Such a row is
    # corrupt, and keeping it would create a record whose event_time sits after
    # its known_at — a record knowable before it happened, which is the one
    # shape the point-in-time layer exists to make impossible.
    if date > accepted:
        logger.debug(
            "form4: dropping %s transaction dated %s, after acceptance %s",
            ticker or "?", date.date(), accepted.date(),
        )
        return []

    coding = txn.find("transactionCoding")
    code = (_text(coding, "transactionCode") or "").strip().upper()
    shares = _parse_float(_value(txn, "transactionShares", within="transactionAmounts"))
    if shares is None:
        return []

    price = _parse_float(_value(txn, "transactionPricePerShare", within="transactionAmounts"))
    ad = (_value(txn, "transactionAcquiredDisposedCode", within="transactionAmounts") or "").strip().upper()
    owned_after = _parse_float(
        _value(txn, "sharesOwnedFollowingTransaction", within="postTransactionAmounts")
    )

    planned = plan_in_footnotes or _flag(coding, "aff10b5One") or _has_plan_prose(txn)

    return [
        InsiderTransaction(
            symbol=ticker,
            issuer_cik=issuer_cik,
            owner_cik=owner["cik"],
            owner_name=owner["name"],
            transaction_date=date,
            accepted_at=accepted,
            code=code,
            acquired=(ad == "A"),
            shares=shares,
            price=price,
            shares_owned_after=owned_after,
            is_director=owner["director"],
            is_officer=owner["officer"],
            is_ten_percent_owner=owner["ten_percent"],
            officer_title=owner["title"],
            is_derivative=derivative,
            planned_10b5_1=planned,
            is_amendment=is_amendment,
            accession=accession,
        )
        for owner in owners
    ]


def _parse_owners(root: ET.Element) -> list[dict]:
    owners: list[dict] = []
    for node in root.findall("reportingOwner"):
        ident = node.find("reportingOwnerId")
        rel = node.find("reportingOwnerRelationship")
        owners.append({
            "cik": _pad_cik(_text(ident, "rptOwnerCik")),
            "name": (_text(ident, "rptOwnerName") or "").strip(),
            "director": _flag(rel, "isDirector"),
            "officer": _flag(rel, "isOfficer"),
            "ten_percent": _flag(rel, "isTenPercentOwner"),
            "title": (_text(rel, "officerTitle") or "").strip(),
        })
    return owners


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _text(node: Optional[ET.Element], tag: str) -> Optional[str]:
    if node is None:
        return None
    found = node.find(tag)
    return found.text if found is not None and found.text else None


def _value(node: Optional[ET.Element], tag: str, within: Optional[str] = None) -> Optional[str]:
    """Read a `<tag><value>x</value></tag>` pair.

    Form 4 wraps nearly every leaf in a `<value>` element so a footnote
    reference can sit alongside it. Some filers omit the wrapper, so both
    shapes are accepted.
    """
    if node is None:
        return None
    scope = node if within is None else node.find(within)
    if scope is None:
        # Some schema versions flatten these containers; fall back to the
        # whole element rather than reporting the field as absent.
        scope = node
    found = scope.find(tag)
    if found is None:
        return None
    inner = found.find("value")
    if inner is not None and inner.text:
        return inner.text
    return found.text


def _flag(node: Optional[ET.Element], tag: str) -> bool:
    """A boolean field, which filers write as 1/0, true/false, or Y/N."""
    raw = (_value(node, tag) or "").strip().lower()
    return raw in {"1", "true", "yes", "y"}


def _parse_float(raw: Optional[str]) -> Optional[float]:
    if raw is None:
        return None
    try:
        return float(raw.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    """Transaction dates are plain calendar days, kept at UTC midnight.

    No timezone conversion: shifting a date into the previous evening is the
    bug that computed every earnings blackout a day early.
    """
    if not raw:
        return None
    try:
        return datetime.strptime(raw.strip()[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _pad_cik(raw: Optional[str]) -> str:
    if not raw:
        return ""
    digits = raw.strip()
    return f"{int(digits):010d}" if digits.isdigit() else digits


def _has_plan_prose(node: ET.Element) -> bool:
    """Whether any text under this element mentions a 10b5-1 plan."""
    return any(
        _PLAN_PROSE.search(text)
        for text in node.itertext()
        if text and "10" in text
    )


__all__ = [
    "InsiderTransaction",
    "Seniority",
    "parse_form4",
    "PURCHASE_CODE",
    "SALE_CODE",
    "COMPENSATION_CODES",
    "TRANSFER_CODES",
]
