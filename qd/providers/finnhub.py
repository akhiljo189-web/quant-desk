"""
qd.providers.finnhub — the earnings calendar. Implements EarningsSource.

This is the PEAD trigger's data source, which makes it the most
leak-sensitive adapter in the system: everything the strategy is allowed to do
descends from an earnings record, and an earnings record with a careless
timestamp hands the backtest the future.

Finnhub's `/calendar/earnings` returns the CURRENT state of each event. Fetch a
range that includes last quarter and you get the actual EPS alongside the
scheduled date, in one flat row, with nothing marking which of those two facts
was knowable when. Loaded naively, every backtest would know each quarter's
results from the moment the date was announced — weeks early — and the drift
would look perfectly predictable because it *was*, to that backtest.

So the row is split across two timestamps:

    scheduled_known_at   when the DATE became public
    released_at          when the NUMBERS hit the wire

and `EarningsEvent.has_actuals_at()` gates the second independently of the
first. The blackout can see tomorrow's scheduled report today; the PEAD trigger
cannot see its EPS until the release instant has passed.

⚠ `scheduled_known_at` IS AN ASSUMPTION when backfilling history. Finnhub does
not report when a company announced its earnings date, so historical loads
assume `SCHEDULE_LEAD` days ahead of the report. Companies typically confirm
2–4 weeks out, so the default is deliberately generous: assuming we knew EARLY
makes the blackout fire early, which removes trades from the backtest. The
opposite error — assuming we learned late — would let the backtest hold
positions through prints that live trading would have refused, and that is the
mistake with a fat tail attached. In live operation the recorder's arrival time
supersedes this guess entirely.
"""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Mapping, Optional, Sequence

from qd.clock import ET
from qd.config import ProviderConfig
from qd.providers.base import EarningsSource, ProviderError
from qd.providers.http import HttpClient, RateLimiter, Recorder
from qd.types import UTC, EarningsEvent, ensure_utc

logger = logging.getLogger(__name__)

# How far ahead of a report we assume the date was public, when backfilling.
SCHEDULE_LEAD = timedelta(days=21)

# Finnhub's `hour` field -> our session marker. Anything unrecognised becomes
# "dmt" (during market hours), which `expected_release_time` treats as a
# midday release — the worst case for a blackout, and therefore the right
# default when the field is missing or garbled.
_SESSION_MAP: Mapping[str, str] = {
    "bmo": "bmo",     # before market open
    "amc": "amc",     # after market close
    "dmh": "dmt",     # during market hours
    "dmt": "dmt",
}


def _release_instant(report_date: datetime, session: str) -> datetime:
    """When the numbers are assumed to have hit the wire.

    Mirrors `features.earnings.expected_release_time` but works from the raw
    parsed row. The date component is read directly from a UTC-midnight
    datetime — converting to Eastern first would roll it back a day, which is
    the bug that previously computed every blackout for the wrong session.
    """
    d = report_date.date()
    if session == "bmo":
        return datetime.combine(d, time(7, 0), tzinfo=ET).astimezone(UTC)
    if session == "amc":
        return datetime.combine(d, time(16, 15), tzinfo=ET).astimezone(UTC)
    return datetime.combine(d, time(12, 0), tzinfo=ET).astimezone(UTC)


def parse_row(
    row: dict,
    schedule_lead: timedelta = SCHEDULE_LEAD,
    known_at: Optional[datetime] = None,
) -> Optional[EarningsEvent]:
    """Convert one Finnhub calendar row into an EarningsEvent.

    `known_at` overrides the schedule assumption — pass the recorder's arrival
    time when replaying a live capture, where the real answer is known.
    Returns None for rows that cannot be trusted rather than guessing.
    """
    symbol = (row.get("symbol") or "").upper()
    raw_date = row.get("date")
    if not symbol or not raw_date:
        return None

    try:
        report_date = datetime.strptime(raw_date, "%Y-%m-%d").replace(tzinfo=UTC)
    except (ValueError, TypeError):
        logger.debug("finnhub: unparseable date %r for %s", raw_date, symbol)
        return None

    session = _SESSION_MAP.get(str(row.get("hour", "")).lower().strip(), "dmt")

    def num(key: str) -> Optional[float]:
        v = row.get(key)
        if v is None or v == "":
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    eps_actual = num("epsActual")
    rev_actual = num("revenueActual")

    # released_at is set ONLY when actuals are present. A scheduled-but-
    # unreported event must carry released_at=None, because that is what
    # has_actuals_at() keys off — set it eagerly and every future quarter
    # becomes readable the moment its date is announced.
    released_at = (
        _release_instant(report_date, session)
        if (eps_actual is not None or rev_actual is not None) else None
    )

    quarter, year = row.get("quarter"), row.get("year")
    fiscal = f"Q{quarter} {year}" if quarter and year else ""

    return EarningsEvent(
        symbol=symbol,
        report_date=report_date,
        session=session,
        scheduled_known_at=ensure_utc(known_at) if known_at else report_date - schedule_lead,
        eps_estimate=num("epsEstimate"),
        eps_actual=eps_actual,
        revenue_estimate=num("revenueEstimate"),
        revenue_actual=rev_actual,
        released_at=released_at,
        fiscal_period=fiscal,
    )


class FinnhubEarnings:
    """Finnhub earnings-calendar adapter."""

    def __init__(
        self,
        cfg: ProviderConfig,
        schedule_lead: timedelta = SCHEDULE_LEAD,
    ) -> None:
        if not cfg.finnhub_api_key:
            raise ProviderError("FINNHUB_API_KEY is not set")
        self.cfg = cfg
        self.schedule_lead = schedule_lead
        self.http = HttpClient(
            base_url=cfg.finnhub_base,
            headers={"X-Finnhub-Token": cfg.finnhub_api_key},
            timeout=cfg.request_timeout,
            max_retries=cfg.max_retries,
            # Finnhub's free tier allows 60 calls/minute. Staying under it
            # matters more than speed here: a 429 mid-range returns a partial
            # calendar, which downstream is indistinguishable from a quarter
            # where nobody reported.
            rate_limiter=RateLimiter(min(cfg.rate_limit_per_min, 55)),
            recorder=Recorder(cfg.cache_dir, cfg.record_responses),
        )

    def earnings(
        self, symbols: Sequence[str], start: datetime, end: datetime
    ) -> list[EarningsEvent]:
        """Scheduled and reported earnings for `symbols` in the date range.

        One request covers the whole range for ALL companies, then filters
        locally. Per-symbol calls would be 35 requests where one suffices, and
        on a 60/minute budget that difference decides whether a universe scan
        fits inside a decision cycle.
        """
        start, end = ensure_utc(start), ensure_utc(end)
        want = {s.upper() for s in symbols}

        payload = self.http.get(
            "/calendar/earnings",
            {"from": start.strftime("%Y-%m-%d"), "to": end.strftime("%Y-%m-%d")},
            tag="earnings",
        )
        if not payload:
            return []

        rows = payload.get("earningsCalendar") or []
        out: list[EarningsEvent] = []
        for row in rows:
            if (row.get("symbol") or "").upper() not in want:
                continue
            ev = parse_row(row, self.schedule_lead)
            if ev is not None:
                out.append(ev)

        logger.info(
            "finnhub: %d/%d calendar rows matched the universe (%s..%s)",
            len(out), len(rows), start.date(), end.date(),
        )
        return sorted(out, key=lambda e: e.report_date)

    def for_symbol(
        self, symbol: str, start: datetime, end: datetime
    ) -> list[EarningsEvent]:
        """Single-symbol query. Use `earnings()` for a universe — this exists
        for spot checks and debugging, not for scanning."""
        start, end = ensure_utc(start), ensure_utc(end)
        payload = self.http.get(
            "/calendar/earnings",
            {
                "from": start.strftime("%Y-%m-%d"),
                "to": end.strftime("%Y-%m-%d"),
                "symbol": symbol.upper(),
            },
            tag=f"earnings-{symbol}",
        )
        if not payload:
            return []
        out = [
            ev for ev in (
                parse_row(r, self.schedule_lead)
                for r in (payload.get("earningsCalendar") or [])
            ) if ev is not None
        ]
        return sorted(out, key=lambda e: e.report_date)


__all__ = ["FinnhubEarnings", "parse_row", "SCHEDULE_LEAD"]
