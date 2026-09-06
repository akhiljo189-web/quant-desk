"""
qd.providers.fundamentals — company identity, financials, and management's words.

Everything the screener attaches to a candidate once the insider signal has
surfaced it: what kind of entity this is, whether the business is growing, and
what management themselves said about it last quarter.

All of it from EDGAR, all free, all precisely dated.

THREE THINGS THAT LOOK EASY AND ARE NOT

  THE REVENUE TAG CHANGED   `Revenues` is the obvious XBRL concept and Intel
                            does not use it. ASC 606 (2018) moved most filers
                            to `RevenueFromContractWithCustomerExcludingAssessed
                            Tax`, and others report only a segment-level or
                            net-of-returns variant. A reader keyed on the
                            obvious tag reports NO REVENUE for most of the
                            modern market — which does not look like a bug, it
                            looks like a company with no sales. Every figure
                            here comes from a fallback CHAIN, and the concept
                            actually used is recorded on the result.

  FUNDS ARE NOT COMPANIES   A screener over "insider buying" will surface fund
                            share classes, trusts and ETFs, whose filings look
                            structurally identical to an operating company's.
                            One appeared in the first real scan claiming a
                            $1.49bn insider purchase. The filter is the SEC's
                            own `entityType` and SIC code, not name matching —
                            plenty of real companies have "Trust" or "Partners"
                            in their name.

  MANAGEMENT QUOTES ARE     Earnings call transcripts are paywalled, but the
  ALREADY IN THE FILINGS    press release attached to an 8-K item 2.02 is free,
                            carries the CEO's own words, and is timestamped to
                            the second. It is a better source than an interview
                            for this purpose: a CEO can be loose on a podcast
                            and cannot be loose in an SEC filing.

WHAT THIS IS NOT FOR

Nothing here is a signal. It is CONTEXT attached to a candidate a human is
about to research — the design (§4) is explicit that forward estimates and
narrative are not backtestable, and none of this is fed to a score.
"""

from __future__ import annotations

import html
import json
import logging
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence

from qd.types import UTC, ensure_utc

logger = logging.getLogger(__name__)

SEC_DATA = "https://data.sec.gov"
SEC_WWW = "https://www.sec.gov"

# SIC codes for investment vehicles. A company here is a fund, a trust or a
# blank-cheque shell, not an operating business.
FUND_SIC_CODES = frozenset({
    "6722",  # management investment offices, open-end
    "6726",  # investment offices NEC — most ETFs and closed-end funds
    "6770",  # blank checks (SPACs)
    "6799",  # investors NEC
    "6221",  # commodity contracts brokers
})

# Revenue, in the order the SEC's own filers prefer. The first that resolves
# wins, and which one it was is recorded — a figure whose provenance is unknown
# cannot be compared across companies.
REVENUE_CONCEPTS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
    "SalesRevenueGoodsNet",
)
GROSS_PROFIT_CONCEPTS = ("GrossProfit",)
OPERATING_INCOME_CONCEPTS = ("OperatingIncomeLoss",)
NET_INCOME_CONCEPTS = ("NetIncomeLoss", "ProfitLoss")
ASSETS_CONCEPTS = ("Assets",)
DEBT_CONCEPTS = (
    "LongTermDebtNoncurrent", "LongTermDebt", "DebtLongtermAndShorttermCombinedAmount",
)
CASH_CONCEPTS = (
    "CashAndCashEquivalentsAtCarryingValue",
    "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
)
RND_CONCEPTS = ("ResearchAndDevelopmentExpense",)


@dataclass(frozen=True, slots=True)
class CompanyProfile:
    """Who this filer is, and whether it is an operating business at all."""
    cik: str
    name: str
    tickers: tuple[str, ...] = ()
    exchanges: tuple[str, ...] = ()
    sic: str = ""
    sic_description: str = ""
    entity_type: str = ""

    @property
    def symbol(self) -> str:
        return self.tickers[0] if self.tickers else ""

    @property
    def is_fund(self) -> bool:
        return self.sic in FUND_SIC_CODES

    @property
    def is_operating_company(self) -> bool:
        """Whether this is a business rather than an investment vehicle.

        Uses the SEC's own classification. Name matching would be worse in both
        directions: plenty of real companies are called "... Trust" or
        "... Partners", and plenty of funds are not.
        """
        return not self.is_fund and bool(self.tickers)

    @property
    def non_operating_reason(self) -> str:
        """Why this filer is not a screening candidate, or "" if it is.

        Stated precisely: "no listed ticker" and "an investment vehicle" are
        different exclusions, and a message quoting an empty SIC code tells a
        reader nothing about which one fired.
        """
        if self.is_fund:
            return (f"investment vehicle (SIC {self.sic}"
                    + (f" {self.sic_description}" if self.sic_description else "") + ")")
        if not self.tickers:
            return "no listed ticker"
        return ""


@dataclass(frozen=True, slots=True)
class Metric:
    """One reported figure, with the concept it came from and when it was filed.

    The concept is carried because the same idea has several XBRL names and
    they are not always comparable. A revenue figure whose provenance is
    unknown cannot be set beside another company's.
    """
    value: float
    concept: str
    period_end: datetime
    filed: datetime
    fiscal_period: str = ""


@dataclass(frozen=True, slots=True)
class Fundamentals:
    """A company's recent financial shape, as filed."""
    cik: str
    revenue: tuple[Metric, ...] = ()
    gross_profit: tuple[Metric, ...] = ()
    operating_income: tuple[Metric, ...] = ()
    net_income: tuple[Metric, ...] = ()
    assets: tuple[Metric, ...] = ()
    debt: tuple[Metric, ...] = ()
    cash: tuple[Metric, ...] = ()
    rnd: tuple[Metric, ...] = ()
    missing: tuple[str, ...] = ()

    @property
    def revenue_growth_yoy(self) -> Optional[float]:
        """Latest annual revenue against the year before, as a fraction."""
        return _growth(self.revenue)

    @property
    def revenue_acceleration(self) -> Optional[float]:
        """Whether growth is speeding up — the brief's sharpest single idea.

        The second difference: this year's growth rate minus last year's. A
        company going 8% -> 14% -> 23% is a different proposition from one
        going 23% -> 14% -> 8%, and the LEVEL of growth cannot tell them apart.
        """
        if len(self.revenue) < 3:
            return None
        recent = _growth(self.revenue[:2] if len(self.revenue) >= 2 else ())
        prior = _growth(self.revenue[1:3])
        if recent is None or prior is None:
            return None
        return recent - prior

    @property
    def gross_margin(self) -> Optional[float]:
        return _ratio(self.gross_profit, self.revenue)

    @property
    def operating_margin(self) -> Optional[float]:
        return _ratio(self.operating_income, self.revenue)

    @property
    def net_debt(self) -> Optional[float]:
        """Debt less cash. Negative means a net cash position."""
        d = self.debt[0].value if self.debt else None
        c = self.cash[0].value if self.cash else None
        if d is None and c is None:
            return None
        return (d or 0.0) - (c or 0.0)

    @property
    def rnd_intensity(self) -> Optional[float]:
        return _ratio(self.rnd, self.revenue)

    @property
    def latest_filed(self) -> Optional[datetime]:
        for series in (self.revenue, self.net_income, self.assets):
            if series:
                return series[0].filed
        return None


@dataclass(frozen=True, slots=True)
class ManagementCommentary:
    """What management actually said, from the earnings press release.

    Free, timestamped to the second, and legally consequential — which is more
    than can be said for an interview. Quotes are extracted verbatim and never
    summarised or scored: the design forbids narrative from reaching any
    ranking, and a quote a human reads is worth more than a sentiment number
    they cannot audit.
    """
    cik: str
    filed_at: Optional[datetime] = None
    source_url: str = ""
    quotes: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.quotes)


# ─────────────────────────────────────────────────────────────────────────────
# Fetching
# ─────────────────────────────────────────────────────────────────────────────

class EdgarCompany:
    """Reads profile, fundamentals and commentary for one company."""

    def __init__(self, user_agent: str, timeout: int = 30) -> None:
        if "@" not in (user_agent or ""):
            raise ValueError(
                "SEC requires a User-Agent containing a contact email, "
                "e.g. 'quant-desk research you@example.com'")
        self.ua = user_agent
        self.timeout = timeout

    def _get(self, url: str) -> bytes:
        req = urllib.request.Request(url, headers={"User-Agent": self.ua})
        return urllib.request.urlopen(req, timeout=self.timeout).read()

    def _json(self, url: str) -> Optional[dict]:
        try:
            return json.loads(self._get(url))
        except Exception as exc:
            logger.debug("fundamentals: %s failed: %s", url, exc)
            return None

    def profile(self, cik: str) -> Optional[CompanyProfile]:
        p = self._json(f"{SEC_DATA}/submissions/CIK{_pad(cik)}.json")
        if not p:
            return None
        return CompanyProfile(
            cik=_pad(cik),
            name=p.get("name", ""),
            tickers=tuple(t for t in (p.get("tickers") or []) if t),
            exchanges=tuple(e for e in (p.get("exchanges") or []) if e),
            sic=str(p.get("sic") or ""),
            sic_description=p.get("sicDescription", ""),
            entity_type=p.get("entityType", ""),
        )

    def fundamentals(self, cik: str, *, as_of: Optional[datetime] = None) -> Fundamentals:
        """Annual figures, newest first, filtered to what was filed by `as_of`.

        Annual rather than quarterly on purpose: quarterly revenue is seasonal,
        and comparing Q3 to Q2 measures the calendar rather than the business.
        """
        facts = self._json(f"{SEC_DATA}/api/xbrl/companyfacts/CIK{_pad(cik)}.json")
        if not facts:
            return Fundamentals(cik=_pad(cik), missing=("companyfacts unavailable",))
        us = (facts.get("facts") or {}).get("us-gaap") or {}
        cut = ensure_utc(as_of) if as_of else None

        got, missing = {}, []
        for field_name, concepts, is_instant in (
            ("revenue", REVENUE_CONCEPTS, False),
            ("gross_profit", GROSS_PROFIT_CONCEPTS, False),
            ("operating_income", OPERATING_INCOME_CONCEPTS, False),
            ("net_income", NET_INCOME_CONCEPTS, False),
            # Balance-sheet items — measured at a date, not over one.
            ("assets", ASSETS_CONCEPTS, True),
            ("debt", DEBT_CONCEPTS, True),
            ("cash", CASH_CONCEPTS, True),
            ("rnd", RND_CONCEPTS, False),
        ):
            series = _pick_concept(us, concepts, cut, instant=is_instant)
            got[field_name] = series
            if not series:
                missing.append(field_name)
        return Fundamentals(cik=_pad(cik), missing=tuple(missing), **got)

    def commentary(self, cik: str, *, max_quotes: int = 3) -> ManagementCommentary:
        """Management quotes from the most recent earnings press release.

        Finds the latest 8-K tagged item 2.02 — the SEC's own code for
        "Results of Operations", so identifying it is a label lookup rather
        than a guess — then reads its exhibit and extracts attributed quotes.
        """
        subs = self._json(f"{SEC_DATA}/submissions/CIK{_pad(cik)}.json")
        if not subs:
            return ManagementCommentary(cik=_pad(cik))
        rec = (subs.get("filings") or {}).get("recent") or {}
        n = len(rec.get("form", []))
        for i in range(n):                       # newest first in EDGAR order
            if rec["form"][i] != "8-K":
                continue
            if "2.02" not in (rec.get("items", [""] * n)[i] or ""):
                continue
            acc = (rec.get("accessionNumber", [""] * n)[i] or "").replace("-", "")
            if not acc:
                continue
            base = f"{SEC_WWW}/Archives/edgar/data/{int(cik)}/{acc}"
            manifest = self._json(f"{base}/index.json") or {}
            names = [it["name"] for it in
                     (manifest.get("directory") or {}).get("item", [])]
            # EX-99.1 is the press release by convention; fall back to any
            # exhibit rather than returning nothing.
            # Filenames are free-form: "ex-99_1.htm", "exhibit991earnings-.htm",
            # "a8-kex991.htm". Match "99" near "ex" anywhere in the name rather
            # than assuming a separator.
            exhibits = [x for x in names if re.search(r"ex.{0,10}99", x, re.I)] or \
                       [x for x in names if x.lower().endswith((".htm", ".html"))]
            for name in exhibits[:2]:
                try:
                    text = _strip_html(self._get(f"{base}/{name}").decode("utf-8", "replace"))
                except Exception:
                    continue
                quotes = _extract_quotes(text, max_quotes)
                if quotes:
                    return ManagementCommentary(
                        cik=_pad(cik),
                        filed_at=_parse_date(rec.get("filingDate", [""] * n)[i]),
                        source_url=f"{base}/{name}",
                        quotes=quotes,
                    )
            break
        return ManagementCommentary(cik=_pad(cik))


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _pick_concept(
    us_gaap: dict,
    concepts: Sequence[str],
    cut: Optional[datetime],
    *,
    instant: bool = False,
) -> tuple[Metric, ...]:
    """First concept in the chain that yields data.

    The chain exists because the same idea has several XBRL names — `Revenues`
    is the obvious one and most modern filers do not use it.

    `instant` selects BALANCE-SHEET facts, which are a different shape from
    income-statement ones: assets, debt and cash are measured AT a date and
    carry no `start`, so the annual-duration filter that keeps a 10-K's
    quarterly rows out rejects every one of them. Reading a balance sheet with
    the income-statement rule returns nothing at all, which looks like a
    company with no assets.
    """
    for concept in concepts:
        node = us_gaap.get(concept)
        if not node:
            continue
        rows = []
        for unit_rows in (node.get("units") or {}).values():
            for r in unit_rows:
                if r.get("form") not in ("10-K", "10-K/A", "10-Q", "20-F"):
                    continue
                end, filed = _parse_date(r.get("end")), _parse_date(r.get("filed"))
                if end is None or filed is None:
                    continue
                if instant:
                    # A balance-sheet fact has no start; the latest one wins.
                    if r.get("start") is not None:
                        continue
                else:
                    if r.get("fp") != "FY" or r.get("form") == "10-Q":
                        continue
                    start = _parse_date(r.get("start"))
                    if start is None:
                        continue
                    # Annual spans only. A 10-K also carries quarterly and
                    # cumulative rows under the same tag.
                    if not 300 <= (end - start).days <= 400:
                        continue
                if cut and filed > cut:
                    continue
                rows.append(Metric(value=float(r["val"]), concept=concept,
                                   period_end=end, filed=filed,
                                   fiscal_period=str(r.get("fy") or "")))
        if rows:
            # Keep the FIRST filing of each period — a restatement was not
            # available at the time, the same rule providers/xbrl.py applies.
            first_by_period: dict[datetime, Metric] = {}
            for m in sorted(rows, key=lambda x: x.filed):
                first_by_period.setdefault(m.period_end, m)
            return tuple(sorted(first_by_period.values(),
                                key=lambda m: m.period_end, reverse=True))
    return ()


def _growth(series: Sequence[Metric]) -> Optional[float]:
    if len(series) < 2 or series[1].value == 0:
        return None
    return (series[0].value - series[1].value) / abs(series[1].value)


def _ratio(num: Sequence[Metric], den: Sequence[Metric]) -> Optional[float]:
    if not num or not den or den[0].value == 0:
        return None
    # Only compare figures covering the same period, or the ratio is nonsense.
    if num[0].period_end != den[0].period_end:
        return None
    return num[0].value / den[0].value


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
# A quoted sentence followed by attribution, or attribution then a quote.
# A speaker's name: capitalised words, allowing middle initials, terminated by
# the comma or full stop before their title. Matching lazily up to the first
# "." truncates "Henry A. Fernandez" to "Henry A" — the initial's own period
# ends the match.
_NAME = r"([A-Z][\w.'\-]*(?:\s+[A-Z][\w.'\-]*){0,4})\s*[,.]"
# Quote marks: straight, curly, and the single-quote variants filers use.
# Press releases are written in Word and almost always emit curly quotes, so a
# class of only ASCII quotes matches nothing on a real filing.
_Q = "[\u201c\u201d\"\u2018\u2019']"
_BODY = "([^\u201c\u201d\"\u2018\u2019']{60,600})"
_SAID = r"(?:said|added|commented|stated|according to)"

_QUOTE = re.compile(
    f"{_Q}{_BODY}{_Q}" + r"[,.]?\s*" + _SAID + r"\s+" + _NAME
    + "|" + _SAID + r"\s+" + _NAME + r"\s*" + f"{_Q}{_BODY}{_Q}",
)


def _strip_html(raw: str) -> str:
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", raw))).strip()


def _extract_quotes(text: str, limit: int) -> tuple[str, ...]:
    """Verbatim quotes with a named speaker.

    Attribution is required. An unattributed sentence in a press release is
    marketing copy; a quote with a name attached is a person on the record.
    """
    out: list[str] = []
    for m in _QUOTE.finditer(text):
        quote, who = (m.group(1), m.group(2)) if m.group(1) else (m.group(4), m.group(3))
        if not quote or not who:
            continue
        cleaned = _WS.sub(" ", quote).strip()
        if cleaned and cleaned not in out:
            out.append(f"{who.strip()}: “{cleaned}”")
        if len(out) >= limit:
            break
    return tuple(out)


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw[:10], "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _pad(cik: str) -> str:
    s = str(cik).strip()
    return f"{int(s):010d}" if s.isdigit() else s


__all__ = [
    "CompanyProfile", "Fundamentals", "Metric", "ManagementCommentary",
    "EdgarCompany", "FUND_SIC_CODES", "REVENUE_CONCEPTS",
]
