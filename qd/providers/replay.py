"""
qd.providers.replay — the point-in-time provider.

This module is the structural defence against look-ahead. It holds a complete
dataset and serves only the records whose `known_at` has already passed on the
simulated clock. The engine asks for "bars up to now" exactly as it does live;
what "now" means is the only difference.

Why enforce it here rather than trusting each feature module to be careful:
look-ahead is not a bug you avoid by concentrating. It is the default state of
any code holding a full history, it produces no error, and it makes results
better rather than worse — so nothing about the output invites suspicion. The
only reliable defence is a single choke point that cannot serve the future, and
a test that proves it. See `tests/test_pointintime.py`.

`strict=True` goes further and RAISES when asked for a range extending past the
clock. Any such request is a bug in the caller, and in strict mode it fails
loudly during development rather than silently returning a truncated list that
looks like an ordinary quiet period.
"""

from __future__ import annotations

import bisect
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from qd.clock import SimClock
from qd.providers.base import ProviderError
from qd.types import (
    Bar, EarningsEvent, NewsItem, OptionTrade, Quote, ensure_utc,
)

logger = logging.getLogger(__name__)


class LookAheadError(ProviderError):
    """Raised in strict mode when a caller asks for data from the future."""


@dataclass
class ReplayDataset:
    """Everything the simulation may eventually reveal.

    Records go in unordered and are indexed by `known_at` on freeze.
    """
    bars: dict[str, list[Bar]] = field(default_factory=dict)
    daily: dict[str, list[Bar]] = field(default_factory=dict)
    quotes: dict[str, list[Quote]] = field(default_factory=dict)
    news: list[NewsItem] = field(default_factory=list)
    earnings: list[EarningsEvent] = field(default_factory=list)
    option_trades: dict[str, list[OptionTrade]] = field(default_factory=dict)

    def add_bars(self, symbol: str, bars: Iterable[Bar]) -> None:
        self.bars.setdefault(symbol.upper(), []).extend(bars)

    def add_daily(self, symbol: str, bars: Iterable[Bar]) -> None:
        self.daily.setdefault(symbol.upper(), []).extend(bars)

    def add_quotes(self, symbol: str, quotes: Iterable[Quote]) -> None:
        self.quotes.setdefault(symbol.upper(), []).extend(quotes)

    def add_news(self, items: Iterable[NewsItem]) -> None:
        self.news.extend(items)

    def add_earnings(self, events: Iterable[EarningsEvent]) -> None:
        self.earnings.extend(events)

    def add_option_trades(self, underlying: str, trades: Iterable[OptionTrade]) -> None:
        self.option_trades.setdefault(underlying.upper(), []).extend(trades)

    def freeze(self) -> None:
        """Sort every series by `known_at` so lookups can binary-search."""
        for d in (self.bars, self.daily, self.quotes, self.option_trades):
            for k in d:
                d[k].sort(key=lambda r: r.known_at)
        self.news.sort(key=lambda r: r.known_at)
        self.earnings.sort(key=lambda r: r.known_at)

    def span(self) -> Optional[tuple[datetime, datetime]]:
        """Earliest and latest `known_at` across everything."""
        times: list[datetime] = []
        for d in (self.bars, self.daily, self.quotes, self.option_trades):
            for series in d.values():
                if series:
                    times.append(series[0].known_at)
                    times.append(series[-1].known_at)
        for series in (self.news, self.earnings):
            if series:
                times.append(series[0].known_at)
                times.append(series[-1].known_at)
        return (min(times), max(times)) if times else None

    def symbols(self) -> list[str]:
        return sorted(set(self.bars) | set(self.daily))

    def summary(self) -> str:
        return (
            f"{len(self.symbols())} symbols, "
            f"{sum(len(v) for v in self.bars.values())} intraday bars, "
            f"{sum(len(v) for v in self.daily.values())} daily bars, "
            f"{len(self.news)} news, {len(self.earnings)} earnings, "
            f"{sum(len(v) for v in self.option_trades.values())} option trades"
        )


def _visible(records: Sequence, now: datetime) -> Sequence:
    """Slice a known_at-sorted sequence to what is visible at `now`."""
    keys = [r.known_at for r in records]
    return records[: bisect.bisect_right(keys, now)]


class ReplayProvider:
    """Serves historical data as of the simulated clock.

    Implements MarketData, NewsFeed, EarningsSource and OptionsTape.
    """

    def __init__(
        self, dataset: ReplayDataset, clock: SimClock, strict: bool = True
    ) -> None:
        self.data = dataset
        self.clock = clock
        self.strict = strict
        self.data.freeze()

    # ── the choke point ──────────────────────────────────────────────────────

    def _bound(self, end: datetime, what: str) -> datetime:
        """Clamp a requested end time to the simulated present.

        Every read passes through here. In strict mode a request beyond `now`
        raises; otherwise it is silently clamped, which is right for production
        callers that pass an open-ended `until`.
        """
        now = self.clock.now()
        end = ensure_utc(end)
        if end > now:
            if self.strict:
                raise LookAheadError(
                    f"{what}: requested data to {end.isoformat()} but the clock "
                    f"is at {now.isoformat()} — this is look-ahead"
                )
            return now
        return end

    # ── MarketData ───────────────────────────────────────────────────────────

    def bars(
        self, symbol: str, start: datetime, end: datetime, minutes: int = 5
    ) -> list[Bar]:
        end = self._bound(end, f"bars({symbol})")
        start = ensure_utc(start)
        series = self.data.bars.get(symbol.upper(), [])
        return [b for b in _visible(series, end) if b.start >= start]

    def daily_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        end = self._bound(end, f"daily_bars({symbol})")
        start = ensure_utc(start)
        series = self.data.daily.get(symbol.upper(), [])
        return [b for b in _visible(series, end) if b.start >= start]

    def quote(self, symbol: str, at: datetime) -> Optional[Quote]:
        at = self._bound(at, f"quote({symbol})")
        series = self.data.quotes.get(symbol.upper(), [])
        vis = _visible(series, at)
        if vis:
            return vis[-1]
        # Fall back to the last closed bar's close as both sides. Deliberately
        # spread-free: a simulation that invents a spread here would be
        # inventing the largest single cost the strategy pays.
        bars = _visible(self.data.bars.get(symbol.upper(), []), at)
        if not bars:
            return None
        px = bars[-1].close
        return Quote(symbol.upper(), bars[-1].end, px, px)

    # ── NewsFeed ─────────────────────────────────────────────────────────────

    def news(
        self, symbols: Sequence[str], since: datetime, until: datetime
    ) -> list[NewsItem]:
        until = self._bound(until, "news")
        since = ensure_utc(since)
        want = {s.upper() for s in symbols}
        return [
            n for n in _visible(self.data.news, until)
            if n.known_at >= since and (not want or want & set(n.symbols))
        ]

    # ── EarningsSource ───────────────────────────────────────────────────────

    def earnings(
        self, symbols: Sequence[str], start: datetime, end: datetime
    ) -> list[EarningsEvent]:
        # Earnings are the subtle case. The SCHEDULE is knowable weeks ahead, so
        # forward-looking requests are legitimate and must not be clamped — the
        # blackout check needs to see tomorrow's report today. What must stay
        # hidden is the ACTUALS, and EarningsEvent.has_actuals_at() gates those
        # separately. Clamping here would break the blackout instead.
        now = self.clock.now()
        want = {s.upper() for s in symbols}
        start, end = ensure_utc(start), ensure_utc(end)
        return [
            e for e in self.data.earnings
            if e.known_at <= now
            and (not want or e.symbol.upper() in want)
            and start <= e.report_date <= end
        ]

    # ── OptionsTape ──────────────────────────────────────────────────────────

    def option_trades(
        self, underlying: str, start: datetime, end: datetime
    ) -> list[OptionTrade]:
        end = self._bound(end, f"option_trades({underlying})")
        start = ensure_utc(start)
        series = self.data.option_trades.get(underlying.upper(), [])
        return [t for t in _visible(series, end) if t.known_at >= start]

    # ── stepping ─────────────────────────────────────────────────────────────

    def next_event_time(self, after: Optional[datetime] = None) -> Optional[datetime]:
        """The next instant at which anything becomes known.

        Event-driven stepping rather than a fixed tick: it skips the empty
        stretches and, more importantly, guarantees the simulation lands exactly
        on each arrival rather than a rounded bar boundary that could place a
        headline a few minutes earlier or later than it really was.
        """
        after = ensure_utc(after or self.clock.now())
        best: Optional[datetime] = None

        def consider(series: Sequence) -> None:
            nonlocal best
            keys = [r.known_at for r in series]
            i = bisect.bisect_right(keys, after)
            if i < len(series):
                t = series[i].known_at
                if best is None or t < best:
                    best = t

        for d in (self.data.bars, self.data.daily, self.data.quotes, self.data.option_trades):
            for series in d.values():
                consider(series)
        consider(self.data.news)
        consider(self.data.earnings)
        return best


__all__ = ["ReplayDataset", "ReplayProvider", "LookAheadError"]
