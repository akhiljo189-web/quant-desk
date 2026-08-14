"""
qd.providers.xbrl — quarterly EPS history from SEC XBRL company facts.

Supplies the depth the free vendor tiers do not: seven-plus years of reported
EPS per company, free, from the source of record. Finnhub's free endpoint
returns four quarters, which across a 35-name universe is far too small a
sample for a walk-forward to say anything.

THE THREE TRAPS IN XBRL DATA, all of which produce plausible numbers:

  YTD VS QUARTERLY   Filers report both. A 10-Q for the third quarter carries
                     a three-month figure AND a nine-month cumulative one, with
                     the same `end` date and no flag distinguishing them. Take
                     the wrong one and Q3 EPS silently becomes three quarters of
                     earnings — roughly a 200% "surprise" out of nothing.
                     Resolved by measuring `end - start` and keeping only spans
                     of about three months.

  RESTATEMENTS       The same period is refiled with corrected values. Later
                     filings are more accurate and were NOT available at the
                     time. Keeping the newest value would hand the backtest a
                     correction published months after the trade. Resolved by
                     keeping the FIRST filing of each period and recording the
                     original `filed` date.

  FISCAL YEARS       Apple's Q1 ends in December, most retailers end in
                     January. Comparing calendar quarters across companies is
                     meaningless. Everything here is anchored to the company's
                     own fiscal sequence, and the seasonal comparison is
                     four quarters back in ITS OWN calendar.

  THE fp FIELD       And the trap inside that one: XBRL's `fy`/`fp` describe
                     the FILING a fact came from, not the period it covers. A
                     10-K restates the prior year's quarters, so those rows
                     arrive tagged `fp="FY"` with three-month spans ending in
                     March, June and September. Matching on that label pairs
                     a Q3 against a Q1 — the same wrong-quarter comparison the
                     label matching was written to prevent, through a
                     different door. Every label here is DERIVED from the
                     period end against the company's own fiscal year end.

The `filed` date on every observation is a genuine point-in-time stamp — the
day the figure entered the public record. The exact announcement instant still
comes from the 8-K (see providers/edgar.py); `filed` is the backstop when no
matching 8-K exists.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from qd.config import ProviderConfig
from qd.providers.base import ProviderError
from qd.providers.edgar import SEC_DATA, EdgarFilings
from qd.providers.http import HttpClient, RateLimiter, Recorder
from qd.types import UTC, ensure_utc

logger = logging.getLogger(__name__)

# US-GAAP concepts for diluted EPS, in preference order. Filers are not fully
# consistent, so several are tried before giving up on a company.
EPS_CONCEPTS = (
    "EarningsPerShareDiluted",
    "EarningsPerShareBasicAndDiluted",
    "EarningsPerShareBasic",
)

# A "quarter" is 3 months, but fiscal quarters run 84–98 days depending on the
# 52/53-week calendar a filer uses. Anything outside this is a half-year,
# nine-month or annual figure wearing a quarter's end date.
MIN_QUARTER_DAYS = 80
MAX_QUARTER_DAYS = 100

# A seasonal difference this many deviations out, in the quarter being compared
# AGAINST, marks the comparison as contaminated. Fixed, not tuned — see
# `compute_sue` for why the winsorizing cap cannot do this job.
CONTAMINATION_SUE = 3.0


ANNUAL_MIN_DAYS = 350
ANNUAL_MAX_DAYS = 380

# 52/53-week fiscal calendars drift a few days either side of a fixed anchor,
# so a "December" year end can land on 2 January. Reading the label from a date
# shifted back a week puts every such variant in the same month.
_DRIFT = timedelta(days=7)


def _anchor(end: datetime) -> datetime:
    return end - _DRIFT


def fiscal_year_end_month(ends: Iterable[datetime]) -> Optional[int]:
    """The company's fiscal year end month, from its annual periods."""
    counts: dict[int, int] = {}
    for e in ends:
        m = _anchor(e).month
        counts[m] = counts.get(m, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]


def fiscal_label(end: datetime, fy_end_month: int) -> Optional[tuple[int, str]]:
    """(fiscal_year, "Qn") for a period ending `end`, or None if off-grid.

    Derived from the date rather than read from XBRL's `fp`, which names the
    filing's period and not the fact's. Q1 is the quarter ending three months
    after the fiscal year end; the fiscal year is the one this quarter closes
    into, so Apple's December quarter is FY Q1 of the following year.
    """
    a = _anchor(end)
    months_after = (a.month - fy_end_month) % 12
    if months_after % 3:
        return None                     # not on the company's quarterly grid
    qi = months_after // 3 or 4
    fy = a.year if a.month <= fy_end_month else a.year + 1
    return fy, f"Q{qi}"


@dataclass(frozen=True)
class Quarter:
    """One reported fiscal quarter, as first published."""
    symbol: str
    start: datetime
    end: datetime
    eps: float
    filed: datetime          # when it entered the public record
    fiscal_year: Optional[int] = None
    fiscal_period: Optional[str] = None      # Q1..Q4
    form: str = ""
    concept: str = ""

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    @property
    def label(self) -> str:
        if self.fiscal_year and self.fiscal_period:
            return f"{self.fiscal_period} {self.fiscal_year}"
        return f"{self.end:%Y-%m}"


class XbrlFacts:
    """Reads quarterly EPS from SEC XBRL."""

    def __init__(self, cfg: ProviderConfig, user_agent: Optional[str] = None) -> None:
        ua = user_agent or cfg.sec_user_agent
        if not ua or "@" not in ua:
            raise ProviderError(
                "SEC requires a User-Agent containing a contact email — set "
                "SEC_USER_AGENT"
            )
        self.cfg = cfg
        self._edgar = EdgarFilings(cfg, ua)
        self._polygon = None            # lazily built, only for split data
        self.http = HttpClient(
            base_url=SEC_DATA,
            headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
            timeout=cfg.request_timeout, max_retries=cfg.max_retries,
            rate_limiter=RateLimiter(300),
            recorder=Recorder(cfg.cache_dir, cfg.record_responses),
        )

    def cik_for(self, symbol: str) -> Optional[str]:
        return self._edgar.cik_for(symbol)

    def quarters(
        self, symbol: str, since: Optional[datetime] = None,
        include_derived_q4: bool = True,
    ) -> list[Quarter]:
        """Quarterly EPS for a symbol, oldest first, as originally filed.

        With `include_derived_q4`, the fourth quarter is reconstructed from the
        annual figure. Without it a quarter of the sample is missing — and
        always the SAME quarter, so year-end effects vanish entirely rather
        than thinning out evenly.
        """
        cik = self.cik_for(symbol)
        if cik is None:
            logger.warning("xbrl: no CIK for %s", symbol)
            return []

        for concept in EPS_CONCEPTS:
            rows = self._concept(cik, concept)
            if not rows:
                continue
            fy_end = self._fiscal_year_end(rows)
            if fy_end is None:
                logger.warning("xbrl: %s — no annual period, cannot label "
                               "quarters", symbol)
                continue
            quarters = self._to_quarters(symbol, rows, concept, since, fy_end)
            if len(quarters) < 8:         # too thin for a seasonal comparison
                continue
            if include_derived_q4:
                annuals = self._to_annuals(symbol, rows, concept, since, fy_end)
                derived = derive_q4(quarters, annuals)
                if derived:
                    quarters = sorted(quarters + derived, key=lambda q: q.end)
                    logger.debug("xbrl: %s derived %d Q4s", symbol, len(derived))
            logger.debug("xbrl: %s using %s (%d quarters)", symbol, concept, len(quarters))
            return quarters
        logger.warning("xbrl: %s — no usable EPS concept", symbol)
        return []

    def splits(self, symbol: str) -> list[tuple[datetime, float]]:
        """(execution_date, ratio) from Polygon, newest first.

        Ratio is shares-after / shares-before: a 1:4 row (split_from=1,
        split_to=4) is a 4:1 split and returns 4.0. Needed because XBRL EPS is
        as-filed, so a seasonal comparison spanning a split has its two sides
        on different share bases.
        """
        from qd.providers.polygon import PolygonProvider

        if self._polygon is None:
            try:
                self._polygon = PolygonProvider(self.cfg)
            except ProviderError:
                logger.warning("splits unavailable: no Polygon key — seasonal "
                               "comparisons spanning a split will be wrong")
                return []
        try:
            payload = self._polygon.http.get(
                "/v3/reference/splits", {"ticker": symbol.upper(), "limit": 100},
                tag=f"splits-{symbol}",
            )
        except ProviderError as exc:
            logger.warning("%s: splits fetch failed: %s", symbol, exc)
            return []

        out: list[tuple[datetime, float]] = []
        for r in (payload or {}).get("results") or []:
            try:
                d = datetime.strptime(r["execution_date"], "%Y-%m-%d").replace(tzinfo=UTC)
                frm, to = float(r.get("split_from", 1)), float(r.get("split_to", 1))
                if frm > 0:
                    out.append((d, to / frm))
            except (KeyError, ValueError, TypeError):
                continue
        return out

    def _concept(self, cik: str, concept: str) -> list[dict]:
        try:
            payload = self.http.get(
                f"/api/xbrl/companyconcept/CIK{cik}/us-gaap/{concept}.json",
                tag=f"xbrl-{concept}",
            )
        except ProviderError:
            return []
        if not payload:
            return []
        units = payload.get("units") or {}
        return units.get("USD/shares") or []

    @staticmethod
    def _fiscal_year_end(rows: Sequence[dict]) -> Optional[int]:
        """The company's fiscal year end month, read from its annual periods."""
        ends = []
        for r in rows:
            try:
                start = datetime.strptime(r["start"], "%Y-%m-%d").replace(tzinfo=UTC)
                end = datetime.strptime(r["end"], "%Y-%m-%d").replace(tzinfo=UTC)
            except (KeyError, ValueError, TypeError):
                continue
            if ANNUAL_MIN_DAYS <= (end - start).days <= ANNUAL_MAX_DAYS:
                ends.append(end)
        return fiscal_year_end_month(ends)

    def _to_annuals(
        self, symbol: str, rows, concept: str, since, fy_end_month: int,
    ) -> list["Quarter"]:
        """Full-year figures, used only to derive the missing Q4."""
        best = {}
        for r in rows:
            try:
                start = datetime.strptime(r["start"], "%Y-%m-%d").replace(tzinfo=UTC)
                end = datetime.strptime(r["end"], "%Y-%m-%d").replace(tzinfo=UTC)
                filed = datetime.strptime(r["filed"], "%Y-%m-%d").replace(tzinfo=UTC)
                val = float(r["val"])
            except (KeyError, ValueError, TypeError):
                continue
            if not (ANNUAL_MIN_DAYS <= (end - start).days <= ANNUAL_MAX_DAYS):
                continue
            if since and end < ensure_utc(since):
                continue
            label = fiscal_label(end, fy_end_month)
            if label is None:
                continue
            prior = best.get(label[0])
            if prior is None or filed < prior.filed:
                best[label[0]] = Quarter(
                    symbol=symbol.upper(), start=start, end=end, eps=val, filed=filed,
                    fiscal_year=label[0], fiscal_period="FY",
                    form=r.get("form", ""), concept=concept,
                )
        return sorted(best.values(), key=lambda q: q.end)

    def _to_quarters(
        self, symbol: str, rows: Sequence[dict], concept: str,
        since: Optional[datetime], fy_end_month: int,
    ) -> list[Quarter]:
        """Filter to genuine three-month periods, first filing of each.

        Keyed on the DERIVED fiscal label rather than on the raw start/end
        pair: the same quarter is re-reported by the 10-Q and again by the next
        10-K, sometimes with start dates a day or two apart, and two records
        wearing one label would leave the seasonal match picking arbitrarily
        between them.
        """
        best: dict[tuple[int, str], Quarter] = {}

        for r in rows:
            start_raw, end_raw, filed_raw = r.get("start"), r.get("end"), r.get("filed")
            if not (start_raw and end_raw and filed_raw):
                continue
            try:
                start = datetime.strptime(start_raw, "%Y-%m-%d").replace(tzinfo=UTC)
                end = datetime.strptime(end_raw, "%Y-%m-%d").replace(tzinfo=UTC)
                filed = datetime.strptime(filed_raw, "%Y-%m-%d").replace(tzinfo=UTC)
                val = float(r["val"])
            except (ValueError, TypeError, KeyError):
                continue

            # The YTD trap: a nine-month figure shares its end date with the
            # three-month one and differs only in span.
            span = (end - start).days
            if not (MIN_QUARTER_DAYS <= span <= MAX_QUARTER_DAYS):
                continue
            if since and end < ensure_utc(since):
                continue

            # The fp trap: `r["fy"]`/`r["fp"]` label the FILING. A 10-K's
            # restated quarters arrive as fp="FY" with March and June end
            # dates, and pairing on that compares a Q3 to a Q1.
            label = fiscal_label(end, fy_end_month)
            if label is None:
                logger.debug("xbrl: %s dropping off-grid period ending %s",
                             symbol, end.date())
                continue

            prior = best.get(label)
            # The restatement trap: keep the EARLIEST filing of a period. A
            # later correction was not available when the trade would have
            # been made.
            if prior is None or filed < prior.filed:
                best[label] = Quarter(
                    symbol=symbol.upper(), start=start, end=end, eps=val, filed=filed,
                    fiscal_year=label[0], fiscal_period=label[1],
                    form=r.get("form", ""), concept=concept,
                )

        return sorted(best.values(), key=lambda q: q.end)


# ─────────────────────────────────────────────────────────────────────────────
# Standardised Unexpected Earnings
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Sue:
    """A seasonal-random-walk surprise, scaled by the company's own history."""
    symbol: str
    quarter: Quarter
    expected: float          # EPS four quarters earlier
    surprise: float          # actual − expected, in EPS terms
    scale: float             # stdev of past seasonal differences
    sue: float               # surprise / scale, winsorized
    history: int             # differences available for the scale estimate
    raw_sue: float = 0.0     # before winsorizing
    winsorized: bool = False # True when the cap bound — treat with less confidence
    contaminated: bool = False   # the year-ago quarter was itself an outlier

    @property
    def label(self) -> str:
        return self.quarter.label

    @property
    def reliable(self) -> bool:
        """False when the reading is more likely an accounting artefact.

        Both flags mean the same thing in practice — the number is extreme for
        a reason the strategy cannot see — and both should keep a trade off
        rather than merely shading its size.
        """
        return not (self.winsorized or self.contaminated)


def seasonal_differences(
    quarters: Sequence[Quarter],
    splits: Optional[Sequence[tuple[datetime, float]]] = None,
) -> list[tuple[Quarter, float]]:
    """(quarter, EPS_q − adjusted EPS_{q−4}) for each quarter with a year-ago match.

    Two corrections, both of which produce entirely plausible garbage if
    skipped.

    MATCHED BY FISCAL LABEL, NOT POSITION. Q4 is routinely absent from XBRL
    (see `derive_q4`), and any gap shifts positional matching so that
    `quarters[i-4]` is no longer the year-ago quarter. Apple's sequence pairs
    2021-12 against 2020-09 that way — a 455-day "annual" comparison. Matching
    on (fiscal_year − 1, fiscal_period) is immune to missing quarters.

    SPLIT-ADJUSTED. XBRL reports EPS as originally filed, so a comparison
    spanning a split has its two sides on different share bases. Apple's 4:1
    in August 2020 turns a flat quarter into a −75% "surprise". The year-ago
    figure is restated onto the current basis, which is also what the market
    saw at the announcement: by then the split had happened and every screen
    quoted the adjusted history.
    """
    by_label: dict[tuple[int, str], Quarter] = {}
    for q in quarters:
        if q.fiscal_year and q.fiscal_period:
            by_label[(int(q.fiscal_year), str(q.fiscal_period))] = q

    out: list[tuple[Quarter, float]] = []
    for q in quarters:
        if not (q.fiscal_year and q.fiscal_period):
            continue
        prior = by_label.get((int(q.fiscal_year) - 1, str(q.fiscal_period)))
        if prior is None:
            continue
        factor = split_factor_between(prior.end, q.end, splits or ())
        out.append((q, q.eps - prior.eps * factor))

    out.sort(key=lambda pair: pair[0].end)
    return out


def split_factor_between(
    earlier: datetime, later: datetime,
    splits: Sequence[tuple[datetime, float]],
) -> float:
    """Multiplier restating an EPS figure from `earlier` onto `later`'s basis.

    A 4:1 split quadruples the share count, so historical per-share earnings
    must be divided by 4 — the factor here is 1/ratio, applied to every split
    executed strictly between the two dates.
    """
    factor = 1.0
    for exec_date, ratio in splits:
        if ratio > 0 and earlier < ensure_utc(exec_date) <= later:
            factor /= ratio
    return factor


def derive_q4(quarters: Sequence[Quarter], annuals: Sequence[Quarter]) -> list[Quarter]:
    """Reconstruct the missing Q4 from the annual figure.

    Filers report Q1–Q3 as three-month figures but disclose Q4 only inside the
    10-K's full-year total, so a naive quarterly pull loses one quarter in four
    — a quarter of the sample, and always the same quarter, which is worse than
    random loss because Q4 carries year-end effects.

        Q4 = FY − (Q1 + Q2 + Q3)

    The derived quarter inherits the 10-K's filing date, which is correct: that
    is when the figure became publicly derivable.
    """
    out: list[Quarter] = []
    by_year: dict[int, list[Quarter]] = {}
    for q in quarters:
        if q.fiscal_year:
            by_year.setdefault(int(q.fiscal_year), []).append(q)

    for annual in annuals:
        fy = annual.fiscal_year
        if not fy:
            continue
        year = by_year.get(int(fy), [])
        if any(q.fiscal_period == "Q4" for q in year):
            continue          # the filer reported it; no need to reconstruct
        parts = [q for q in year if q.fiscal_period in ("Q1", "Q2", "Q3")]
        if len(parts) != 3:
            continue          # incomplete year: deriving would be a guess
        q4_eps = annual.eps - sum(p.eps for p in parts)
        last = max(parts, key=lambda p: p.end)
        out.append(Quarter(
            symbol=annual.symbol,
            start=last.end,
            end=annual.end,
            eps=q4_eps,
            filed=annual.filed,          # knowable only when the 10-K landed
            fiscal_year=int(fy),
            fiscal_period="Q4",
            form=annual.form,
            concept=annual.concept + "+derived",
        ))
    return out


def compute_sue(
    quarters: Sequence[Quarter],
    splits: Optional[Sequence[tuple[datetime, float]]] = None,
    min_history: int = 6,
    winsorize: float = 4.0,
) -> list[Sue]:
    """SUE for each quarter, using only information available at the time.

        SUE = (EPS_q − EPS_{q−4}) / stdev(prior seasonal differences)

    The scaling matters as much as the numerator: a 5c miss is trivial for a
    company whose quarters routinely swing 40c and a serious surprise for one
    that never deviates by more than 3c. Dividing by the company's own history
    of surprises makes the measure comparable across the universe, which is the
    whole point of *standardised* unexpected earnings.

    The scale is computed from differences STRICTLY BEFORE the quarter being
    scored. Using the full-sample standard deviation — the obvious
    implementation — leaks the future into the denominator, which quietly
    shrinks SUE precisely in the periods that later turned volatile.
    """
    diffs = seasonal_differences(quarters, splits)
    out: list[Sue] = []

    # The year-ago seasonal difference, by fiscal label. Used to detect the
    # contamination case below; every entry is a full year older than the
    # quarter that reads it, so nothing here is forward-looking.
    diff_by_label: dict[tuple[int, str], float] = {
        (int(q.fiscal_year), str(q.fiscal_period)): d
        for q, d in diffs if q.fiscal_year and q.fiscal_period
    }

    for i, (q, d) in enumerate(diffs):
        # Prior differences, and "prior" means BY FILING DATE, not by period
        # end. Ordering on period end is the natural reading and is subtly
        # wrong: a derived Q4 carries its 10-K's filing date, so a quarter that
        # ENDED earlier can have entered the public record later. Scoring
        # against it would put a figure in the denominator that had not been
        # published yet — a small leak, in the one direction backtests always
        # fail.
        prior = [x for pq, x in diffs[:i] if pq.filed < q.filed]
        if len(prior) < min_history:
            continue
        scale = statistics.pstdev(prior) if len(prior) > 1 else 0.0
        if scale <= 1e-9:
            continue                  # a company with no surprise variance

        raw = d / scale

        # THE CONTAMINATION CASE, and why the cap below does not cover it.
        #
        # A one-off item — an impairment, a settlement, a tax charge — lands in
        # one quarter and then contaminates the comparison twice: once when it
        # prints, and again a year later when an ordinary quarter is measured
        # against it. CROX booked −8.82 EPS on a writedown; the following year's
        # perfectly normal result reads as an enormous beat.
        #
        # Winsorizing was supposed to blunt that, and it cannot. When a single
        # shock dominates a company's history, it inflates the very denominator
        # used to judge it: with n prior differences, a lone outlier of ANY
        # magnitude scores exactly n/sqrt(n−1). That is 3.6 at n=12 and 3.3 at
        # n=20 — always under a ±4 cap, no matter how violent the item was. The
        # cap fires on diffuse volatility and never on the one thing it was
        # written to catch.
        #
        # So the artefact has to be identified by its shape rather than its
        # size: an extreme difference of the OPPOSITE sign, exactly one year
        # earlier, is the signature of a rebound off a one-off rather than an
        # earnings surprise.
        contaminated = False
        if q.fiscal_year and q.fiscal_period:
            back = diff_by_label.get((int(q.fiscal_year) - 1, str(q.fiscal_period)))
            if back is not None and back * d < 0 and abs(back) / scale >= CONTAMINATION_SUE:
                contaminated = True
                logger.debug(
                    "sue: %s %s contaminated — year-ago diff %.2f (%.1f sd)",
                    q.symbol, q.label, back, abs(back) / scale,
                )

        # Winsorize. Capping at ±4 is standard in the PEAD literature and still
        # worth doing — beyond four deviations the number is more often an
        # accounting event than an earnings surprise, and the strategy has no
        # way to tell them apart. It just is not the defence against the case
        # above; it catches the company whose surprises are broadly volatile,
        # not the one with a single disaster in its history.
        capped = max(-winsorize, min(winsorize, raw))
        out.append(Sue(
            symbol=q.symbol, quarter=q, expected=q.eps - d, surprise=d,
            scale=scale, sue=capped, history=len(prior),
            raw_sue=raw, winsorized=abs(raw) > winsorize,
            contaminated=contaminated,
        ))
    return out


def sue_to_surprise_pct(sue: Sue) -> Optional[float]:
    """Express SUE as a fractional surprise, for the existing EarningsEvent.

    `EarningsEvent.eps_surprise_pct()` divides by |estimate|, so feeding it
    `expected` as the estimate and `eps` as the actual reproduces the same
    ratio the consensus path produced. Keeping the downstream contract
    unchanged means the scoring, decay and gating code needs no edits and no
    re-testing for this change.
    """
    denom = abs(sue.expected)
    if denom < 0.01:
        return None
    return sue.surprise / denom


__all__ = [
    "XbrlFacts", "Quarter", "Sue", "compute_sue", "seasonal_differences",
    "sue_to_surprise_pct", "fiscal_label", "fiscal_year_end_month",
    "EPS_CONCEPTS", "MIN_QUARTER_DAYS", "MAX_QUARTER_DAYS", "CONTAMINATION_SUE",
]
