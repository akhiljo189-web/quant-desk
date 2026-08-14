"""
qd.providers.earnings — the PEAD trigger's data path.

Two sources answer two different questions, and the split is exactly along the
line that matters:

    WHAT happened   the surprise itself. Two implementations:
                    `sue_earnings()`  SEC XBRL, seven-plus years — THE ONE USED
                    `earnings()`      Finnhub consensus, four quarters — kept
                                      only for cross-checking the definitions

    WHEN it became public   SEC EDGAR item-2.02 8-K, to the second.
                            Free. Carries no estimates.

Joining them is not a workaround for a paywall; it produces a better record
than the paid calendar would. The premium feed sells a vendor's transcription
of an announcement date. EDGAR's `acceptanceDateTime` is the announcement
becoming public — the primary record, filed under legal obligation and tagged
by the SEC's own item code.

The two surprise definitions do NOT agree — measured at 65%, and flat as both
become decisive, so they genuinely disagree about a third of the time rather
than differing on noise. `HYPOTHESIS.md` records that measurement and its
consequence: a verdict reached with SUE is a verdict about SUE.

WHY THE JOIN IS THE DANGEROUS PART
Finnhub says a quarter ended 30 June with a 4% beat. Using that date, the
system would start trading the drift in early July — roughly four weeks before
the earnings were announced. No error, plausible-looking output, spectacular
fake backtest. So every row must be matched to a real filing or DISCARDED.
There is no "assume it was reported around the usual time" path here: an
unmatched row is dropped and counted, and `match_report()` surfaces the rate so
a poor join is visible as a number rather than as a suspiciously good result.

The session marker (before open / after close) is likewise derived from the
filing's real acceptance time in Eastern, not from a vendor flag. CROX files
around 07:03 ET, AAPL around 16:30 ET; the data says so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Mapping, Optional, Sequence

from qd.clock import ET
from qd.config import ProviderConfig
from qd.providers.base import ProviderError
from qd.providers.edgar import EdgarFilings, Filing, estimate_next_report
from qd.providers.finnhub import FinnhubEarnings
from qd.providers.http import HttpClient, RateLimiter, Recorder
from qd.providers.xbrl import Sue, XbrlFacts, compute_sue, sue_to_surprise_pct
from qd.types import UTC, EarningsEvent, ensure_utc

logger = logging.getLogger(__name__)

# A results 8-K normally follows the quarter end by two to eight weeks. The
# window is generous at both ends but not unbounded: without an upper bound a
# missing filing would silently match the NEXT quarter's release and shift the
# whole event by three months.
MIN_LAG = timedelta(days=5)
MAX_LAG = timedelta(days=110)


@dataclass(frozen=True)
class MatchStats:
    """How well the join worked. Reported, never assumed."""
    rows: int = 0
    matched: int = 0
    unmatched: int = 0
    no_filings: int = 0
    dropped_unreliable: int = 0

    @property
    def rate(self) -> float:
        return self.matched / self.rows if self.rows else 0.0

    def describe(self) -> str:
        tail = (f"; {self.dropped_unreliable} dropped as accounting artefacts"
                if self.dropped_unreliable else "")
        return (
            f"{self.matched}/{self.rows} earnings rows matched to filings "
            f"({self.rate:.0%}); {self.unmatched} unmatched, "
            f"{self.no_filings} symbols with no EDGAR filings{tail}"
        )


def session_for(accepted_at: datetime) -> str:
    """bmo / amc / dmt, from the real acceptance time in Eastern.

    Derived rather than taken from a vendor flag, so it reflects what the
    company actually did. It drives the blackout window and the post-release
    settling delay, both of which are wrong by hours if the marker is wrong.
    """
    et = ensure_utc(accepted_at).astimezone(ET).time()
    if et < time(9, 30):
        return "bmo"
    if et >= time(16, 0):
        return "amc"
    return "dmt"


def match_report(
    period_end: datetime, filings: Sequence[Filing]
) -> Optional[Filing]:
    """The earnings filing that announced the quarter ending `period_end`.

    Rule: the EARLIEST item-2.02 filing accepted between MIN_LAG and MAX_LAG
    after the period end. Earliest matters — a company may file an 8-K with
    results and later a 10-Q covering the same quarter, and the press release
    is the moment the market learned, not the periodic report.

    Returns None when nothing fits, and the caller must drop the row. Guessing
    a date here is the leak.
    """
    period_end = ensure_utc(period_end)
    lo, hi = period_end + MIN_LAG, period_end + MAX_LAG

    exact = [f for f in filings if f.is_earnings_release and lo <= f.accepted_at <= hi]
    if exact:
        return min(exact, key=lambda f: f.accepted_at)

    # Fallback: companies that report inside the 10-Q/10-K without a separate
    # results 8-K. Less precise — the periodic report can trail the press
    # release — so it is used only when no 2.02 exists for the period.
    periodic = [f for f in filings if f.is_periodic and lo <= f.accepted_at <= hi]
    if periodic:
        return min(periodic, key=lambda f: f.accepted_at)
    return None


class EarningsProvider:
    """Combined EarningsSource. Implements the protocol in providers/base.py."""

    def __init__(
        self,
        cfg: ProviderConfig,
        finnhub: Optional[FinnhubEarnings] = None,
        edgar: Optional[EdgarFilings] = None,
        facts: Optional[XbrlFacts] = None,
    ) -> None:
        self.cfg = cfg
        self.finnhub = finnhub or FinnhubEarnings(cfg)
        self.edgar = edgar or EdgarFilings(cfg)
        # Built on first use, not here: XbrlFacts refuses to construct without
        # a contact User-Agent, and that should not break the consensus path
        # for anyone who never touches XBRL.
        self._facts = facts
        self.last_stats = MatchStats()
        self._surprise_http = HttpClient(
            base_url=cfg.finnhub_base,
            headers={"X-Finnhub-Token": cfg.finnhub_api_key},
            timeout=cfg.request_timeout, max_retries=cfg.max_retries,
            rate_limiter=RateLimiter(min(cfg.rate_limit_per_min, 55)),
            recorder=Recorder(cfg.cache_dir, cfg.record_responses),
        )

    @property
    def facts(self) -> XbrlFacts:
        if self._facts is None:
            self._facts = XbrlFacts(self.cfg)
        return self._facts

    # ── Finnhub surprises ────────────────────────────────────────────────────

    def surprises(self, symbol: str) -> list[dict]:
        """Reported quarters from Finnhub's free /stock/earnings endpoint.

        Returns the last several quarters; the endpoint takes no date range, so
        callers filter. Rows carry `period` (fiscal quarter END — not the
        announcement date), estimate, actual, surprise, surprisePercent.
        """
        payload = self._surprise_http.get(
            "/stock/earnings", {"symbol": symbol.upper()}, tag=f"surprise-{symbol}"
        )
        return payload if isinstance(payload, list) else []

    # ── the join ─────────────────────────────────────────────────────────────

    def earnings(
        self, symbols: Sequence[str], start: datetime, end: datetime
    ) -> list[EarningsEvent]:
        """Earnings events with REAL announcement timestamps.

        Rows that cannot be matched to a filing are dropped, not dated by
        assumption. `last_stats` records how many, and a low match rate is a
        reason to distrust the run rather than to proceed quietly.
        """
        start, end = ensure_utc(start), ensure_utc(end)
        out: list[EarningsEvent] = []
        rows = matched = unmatched = no_filings = 0

        for symbol in symbols:
            sym = symbol.upper()
            try:
                surprises = self.surprises(sym)
            except ProviderError as exc:
                logger.warning("%s: surprise fetch failed: %s", sym, exc)
                continue

            # Widen the filing window past the requested range: a quarter
            # ending just before `start` is announced inside it.
            try:
                filings = self.edgar.earnings_releases(
                    sym, start - timedelta(days=MAX_LAG.days + 30), end + timedelta(days=5)
                )
            except ProviderError as exc:
                logger.warning("%s: EDGAR fetch failed: %s", sym, exc)
                filings = []

            if not filings:
                no_filings += 1
                logger.warning("%s: no EDGAR filings — every row will be dropped", sym)

            releases = sorted(
                (f for f in filings if f.is_earnings_release),
                key=lambda f: f.accepted_at,
            )

            for row in surprises:
                period_raw = row.get("period")
                if not period_raw:
                    continue
                try:
                    period_end = datetime.strptime(period_raw, "%Y-%m-%d").replace(tzinfo=UTC)
                except (ValueError, TypeError):
                    continue

                rows += 1
                filing = match_report(period_end, filings)
                if filing is None:
                    unmatched += 1
                    logger.debug("%s: no filing matched period %s", sym, period_raw)
                    continue
                if not (start <= filing.accepted_at <= end):
                    matched += 1        # matched, just outside the window
                    continue
                matched += 1

                # When the SCHEDULE became knowable. The previous quarter's
                # release is the honest anchor: once a company has reported Q1,
                # the timing of Q2 is predictable from its own cadence. Falls
                # back to a conservative fixed lead when there is no prior
                # release to reason from.
                prior = [f for f in releases if f.accepted_at < filing.accepted_at]
                scheduled_known = (
                    prior[-1].accepted_at if prior
                    else filing.accepted_at - timedelta(days=21)
                )

                def num(key: str) -> Optional[float]:
                    v = row.get(key)
                    try:
                        return float(v) if v is not None and v != "" else None
                    except (TypeError, ValueError):
                        return None

                q, y = row.get("quarter"), row.get("year")
                out.append(EarningsEvent(
                    symbol=sym,
                    # report_date is the calendar day of the ANNOUNCEMENT, not
                    # the quarter end — everything downstream reads it as the
                    # event date.
                    report_date=filing.accepted_at.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ),
                    session=session_for(filing.accepted_at),
                    scheduled_known_at=scheduled_known,
                    eps_estimate=num("estimate"),
                    eps_actual=num("actual"),
                    released_at=filing.accepted_at,     # the exact instant
                    fiscal_period=f"Q{q} {y}" if q and y else "",
                ))

        self.last_stats = MatchStats(rows, matched, unmatched, no_filings)
        logger.info("earnings join: %s", self.last_stats.describe())
        return sorted(out, key=lambda e: e.released_at or e.report_date)

    # ── the trigger's real source: XBRL × EDGAR ──────────────────────────────

    def sue_earnings(
        self, symbols: Sequence[str], start: datetime, end: datetime,
        min_history: int = 6, drop_unreliable: bool = True,
    ) -> list[EarningsEvent]:
        """Earnings events measured by time-series SUE rather than consensus.

        THIS IS THE TRIGGER'S ACTUAL DATA PATH. `earnings()` above stays for
        cross-checking the two definitions against each other; it cannot drive
        a walk-forward because Finnhub's free tier returns four quarters, which
        across the universe is a sample too small to say anything.

        The join is the same and so is its discipline — a quarter that cannot
        be tied to a real item-2.02 filing is DROPPED, never dated by
        assumption. The `filed` date on the XBRL fact is tempting as a
        fallback and is not used: the 10-Q trails the press release by days to
        weeks, so events dated that way would measure drift that had already
        happened. Wrong in the safe direction is still wrong.

        The surprise is expressed through `eps_estimate`/`eps_actual` so the
        record is indistinguishable downstream from the consensus path —
        `eps_surprise_pct()` reproduces `sue_to_surprise_pct()` exactly, and no
        scoring, decay or gating code changes for this switch.
        """
        start, end = ensure_utc(start), ensure_utc(end)
        facts = self.facts
        out: list[EarningsEvent] = []
        rows = matched = unmatched = no_filings = dropped = 0

        for symbol in symbols:
            sym = symbol.upper()
            try:
                quarters = facts.quarters(sym)
            except ProviderError as exc:
                logger.warning("%s: XBRL fetch failed: %s", sym, exc)
                continue
            if len(quarters) < min_history + 5:
                logger.warning("%s: only %d quarters — too thin for SUE",
                               sym, len(quarters))
                continue

            sues = compute_sue(quarters, facts.splits(sym), min_history=min_history)

            try:
                filings = self.edgar.earnings_releases(
                    sym, start - timedelta(days=MAX_LAG.days + 30), end + timedelta(days=5)
                )
            except ProviderError as exc:
                logger.warning("%s: EDGAR fetch failed: %s", sym, exc)
                filings = []
            if not filings:
                no_filings += 1
                logger.warning("%s: no EDGAR filings — every row will be dropped", sym)

            releases = sorted(
                (f for f in filings if f.is_earnings_release),
                key=lambda f: f.accepted_at,
            )

            for u in sues:
                rows += 1
                filing = match_report(u.quarter.end, filings)
                if filing is None:
                    unmatched += 1
                    logger.debug("%s: no filing matched %s", sym, u.label)
                    continue
                matched += 1
                if not (start <= filing.accepted_at <= end):
                    continue
                # An impairment rebound or a saturated reading is an accounting
                # event the strategy cannot distinguish from an earnings
                # surprise — and it arrives wearing maximum conviction, which
                # is the worst possible combination. Drop rather than shade.
                if drop_unreliable and not u.reliable:
                    dropped += 1
                    logger.debug("%s %s: dropped (winsorized=%s contaminated=%s)",
                                 sym, u.label, u.winsorized, u.contaminated)
                    continue

                prior = [f for f in releases if f.accepted_at < filing.accepted_at]
                scheduled_known = (
                    prior[-1].accepted_at if prior
                    else filing.accepted_at - timedelta(days=21)
                )
                out.append(EarningsEvent(
                    symbol=sym,
                    report_date=filing.accepted_at.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    ),
                    session=session_for(filing.accepted_at),
                    scheduled_known_at=scheduled_known,
                    eps_estimate=u.expected,
                    eps_actual=u.quarter.eps,
                    released_at=filing.accepted_at,
                    fiscal_period=u.label,
                ))

        self.last_stats = MatchStats(rows, matched, unmatched, no_filings, dropped)
        logger.info("SUE join: %s", self.last_stats.describe())
        return sorted(out, key=lambda e: e.released_at or e.report_date)

    # ── forward calendar (degraded) ──────────────────────────────────────────

    def estimated_next_report(self, symbol: str, now: datetime) -> Optional[datetime]:
        """Estimated next earnings date, from the company's filing cadence.

        EDGAR cannot look forward, so this stands in for the scheduled calendar
        the live blackout would otherwise use. It is an ESTIMATE: widen the
        blackout around it rather than trusting the day. Being early costs a
        skipped trade; being late means holding through a print.
        """
        try:
            hist = self.edgar.earnings_releases(
                symbol, ensure_utc(now) - timedelta(days=900), ensure_utc(now)
            )
        except ProviderError:
            return None
        return estimate_next_report(hist, now)


__all__ = ["EarningsProvider", "MatchStats", "match_report", "session_for",
           "MIN_LAG", "MAX_LAG", "Sue", "sue_to_surprise_pct"]
