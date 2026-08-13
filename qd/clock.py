"""
qd.clock — US equity market calendar and the injectable clock.

Two jobs.

1. Know when the market is actually open. A stock bot that does not model the
   NYSE calendar will place orders into holidays, size positions off half-day
   volume as if it were a full session, and treat the 04:00 pre-market tape as
   though it had regular-hours liquidity. Holidays are computed by rule rather
   than hardcoded, so the calendar does not silently expire next January.

2. Make "now" an argument. Every module here takes a clock instead of calling
   the system time, so the same code path runs live and in replay. If any
   decision function reads the wall clock directly, replay stops being a
   simulation of the live system and becomes a different program that merely
   resembles it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from enum import Enum
from typing import Optional, Protocol
from zoneinfo import ZoneInfo

from qd.types import UTC, ensure_utc, utcnow

ET = ZoneInfo("America/New_York")

# Session boundaries in Eastern time.
PREMARKET_OPEN = time(4, 0)
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)
AFTERHOURS_CLOSE = time(20, 0)


class Phase(str, Enum):
    CLOSED = "closed"
    PREMARKET = "premarket"
    REGULAR = "regular"
    AFTERHOURS = "afterhours"

    @property
    def is_tradeable(self) -> bool:
        """Extended hours are tradeable but not on equal terms — spreads widen,
        depth thins, and a stop that is sane at noon is noise at 04:30. The
        strategy layer treats these phases very differently; see config."""
        return self is not Phase.CLOSED


# ─────────────────────────────────────────────────────────────────────────────
# Holiday rules
# ─────────────────────────────────────────────────────────────────────────────

def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """nth weekday of a month (weekday: Mon=0). n=-1 gives the last one."""
    if n > 0:
        d = date(year, month, 1)
        offset = (weekday - d.weekday()) % 7
        return d + timedelta(days=offset + 7 * (n - 1))
    nxt = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    d = nxt - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(year: int) -> date:
    """Anonymous Gregorian algorithm. Needed only for Good Friday, which is the
    one NYSE holiday with no fixed date and no weekday rule."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lam = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lam) // 451
    month, day = divmod(h + lam - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _observed(d: date) -> Optional[date]:
    """Weekend-shift a fixed-date holiday. Saturday moves back to Friday,
    Sunday forward to Monday."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def market_holidays(year: int) -> frozenset[date]:
    """NYSE full-day closures for a calendar year."""
    out: set[date] = set()

    # New Year's Day. The NYSE does NOT close the preceding Friday when Jan 1
    # falls on a Saturday — the year simply starts without a holiday.
    ny = date(year, 1, 1)
    if ny.weekday() != 5:
        obs = _observed(ny)
        if obs is not None:
            out.add(obs)

    out.add(_nth_weekday(year, 1, 0, 3))       # MLK Jr Day
    out.add(_nth_weekday(year, 2, 0, 3))       # Washington's Birthday
    out.add(_easter(year) - timedelta(days=2))  # Good Friday
    out.add(_nth_weekday(year, 5, 0, -1))      # Memorial Day

    if year >= 2022:                            # Juneteenth, first observed 2022
        obs = _observed(date(year, 6, 19))
        if obs is not None:
            out.add(obs)

    obs = _observed(date(year, 7, 4))
    if obs is not None:
        out.add(obs)

    out.add(_nth_weekday(year, 9, 0, 1))       # Labor Day
    out.add(_nth_weekday(year, 11, 3, 4))      # Thanksgiving

    obs = _observed(date(year, 12, 25))
    if obs is not None:
        out.add(obs)

    return frozenset(d for d in out if d.year == year)


def early_closes(year: int) -> frozenset[date]:
    """Sessions ending 13:00 ET. Volume in the final hour of these days is a
    fraction of normal, so anything calibrated on full-session volume — RVOL
    especially — has to know about them."""
    hols = market_holidays(year)
    out: set[date] = set()

    # Day after Thanksgiving.
    out.add(_nth_weekday(year, 11, 3, 4) + timedelta(days=1))

    # July 3, when it is itself a weekday trading day.
    j3 = date(year, 7, 3)
    if j3.weekday() < 5 and j3 not in hols:
        out.add(j3)

    # Christmas Eve, likewise.
    c24 = date(year, 12, 24)
    if c24.weekday() < 5 and c24 not in hols:
        out.add(c24)

    return frozenset(d for d in out if d.weekday() < 5 and d not in hols)


# ─────────────────────────────────────────────────────────────────────────────
# Calendar
# ─────────────────────────────────────────────────────────────────────────────

class MarketCalendar:
    """NYSE/Nasdaq session calendar. Results are cached per year."""

    def __init__(self) -> None:
        self._hol: dict[int, frozenset[date]] = {}
        self._early: dict[int, frozenset[date]] = {}

    def holidays(self, year: int) -> frozenset[date]:
        if year not in self._hol:
            self._hol[year] = market_holidays(year)
        return self._hol[year]

    def early_closes(self, year: int) -> frozenset[date]:
        if year not in self._early:
            self._early[year] = early_closes(year)
        return self._early[year]

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays(d.year)

    def is_early_close(self, d: date) -> bool:
        return d in self.early_closes(d.year)

    def close_time(self, d: date) -> time:
        return EARLY_CLOSE if self.is_early_close(d) else REGULAR_CLOSE

    def session_bounds(self, d: date) -> Optional[tuple[datetime, datetime]]:
        """(open, close) in UTC for the regular session, or None if closed.

        Built in Eastern and converted, so DST is handled by the tz database
        rather than by an offset constant that is wrong for half the year.
        """
        if not self.is_trading_day(d):
            return None
        o = datetime.combine(d, REGULAR_OPEN, tzinfo=ET).astimezone(UTC)
        c = datetime.combine(d, self.close_time(d), tzinfo=ET).astimezone(UTC)
        return o, c

    def phase(self, now: datetime) -> Phase:
        et = ensure_utc(now).astimezone(ET)
        d = et.date()
        if not self.is_trading_day(d):
            return Phase.CLOSED
        t, close = et.time(), self.close_time(d)
        if PREMARKET_OPEN <= t < REGULAR_OPEN:
            return Phase.PREMARKET
        if REGULAR_OPEN <= t < close:
            return Phase.REGULAR
        if close <= t < AFTERHOURS_CLOSE:
            return Phase.AFTERHOURS
        return Phase.CLOSED

    def is_open(self, now: datetime) -> bool:
        return self.phase(now) is Phase.REGULAR

    def minutes_since_open(self, now: datetime) -> Optional[float]:
        b = self.session_bounds(ensure_utc(now).astimezone(ET).date())
        if b is None:
            return None
        return (ensure_utc(now) - b[0]).total_seconds() / 60.0

    def minutes_to_close(self, now: datetime) -> Optional[float]:
        b = self.session_bounds(ensure_utc(now).astimezone(ET).date())
        if b is None:
            return None
        return (b[1] - ensure_utc(now)).total_seconds() / 60.0

    def session_minutes(self, d: date) -> float:
        """Length of the regular session — 390 minutes, or 210 on a half day."""
        b = self.session_bounds(d)
        return (b[1] - b[0]).total_seconds() / 60.0 if b else 0.0

    def elapsed_fraction(self, now: datetime) -> float:
        """How far through the regular session we are, clamped to [0, 1].

        RVOL is meaningless without this: a stock trading 400k shares by 09:45
        is extraordinary, and the same 400k by 15:45 is a quiet day.
        """
        now = ensure_utc(now)
        b = self.session_bounds(now.astimezone(ET).date())
        if b is None:
            return 0.0
        total = (b[1] - b[0]).total_seconds()
        if total <= 0:
            return 0.0
        return max(0.0, min(1.0, (now - b[0]).total_seconds() / total))

    def trading_day_key(self, now: datetime) -> str:
        """The trading date this instant belongs to, as YYYY-MM-DD in Eastern.

        Used to bucket day trades for the PDT rule and to roll daily counters.
        Must be Eastern, not UTC: at 21:00 UTC it is already tomorrow in London
        but firmly still today's session in New York.
        """
        return ensure_utc(now).astimezone(ET).date().isoformat()

    def next_trading_day(self, d: date) -> date:
        nxt = d + timedelta(days=1)
        while not self.is_trading_day(nxt):
            nxt += timedelta(days=1)
        return nxt

    def prev_trading_day(self, d: date) -> date:
        prv = d - timedelta(days=1)
        while not self.is_trading_day(prv):
            prv -= timedelta(days=1)
        return prv

    def trading_days_between(self, start: date, end: date) -> list[date]:
        out, d = [], start
        while d <= end:
            if self.is_trading_day(d):
                out.append(d)
            d += timedelta(days=1)
        return out


CALENDAR = MarketCalendar()


# ─────────────────────────────────────────────────────────────────────────────
# Clocks
# ─────────────────────────────────────────────────────────────────────────────

class Clock(Protocol):
    def now(self) -> datetime: ...


class LiveClock:
    """Wall clock, UTC."""

    def now(self) -> datetime:
        return utcnow()


@dataclass
class SimClock:
    """Replay clock. Time only ever moves forward.

    The monotonicity check is not defensive tidiness. A replay loop that
    accidentally steps backwards re-serves records the strategy has already
    consumed, producing duplicate signals and an inflated trade count that
    looks like a busy, profitable system.
    """
    _now: datetime

    def __post_init__(self) -> None:
        self._now = ensure_utc(self._now)

    def now(self) -> datetime:
        return self._now

    def set(self, ts: datetime) -> None:
        ts = ensure_utc(ts)
        if ts < self._now:
            raise ValueError(f"clock moved backwards: {self._now} -> {ts}")
        self._now = ts

    def advance(self, delta: timedelta) -> datetime:
        self.set(self._now + delta)
        return self._now


__all__ = [
    "ET", "Phase", "MarketCalendar", "CALENDAR", "Clock", "LiveClock", "SimClock",
    "market_holidays", "early_closes",
    "PREMARKET_OPEN", "REGULAR_OPEN", "REGULAR_CLOSE", "EARLY_CLOSE", "AFTERHOURS_CLOSE",
]
