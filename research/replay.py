"""
research.replay — walk the engine through history.

This runs THE ENGINE, not a reimplementation of it. The same
`Engine.cycle()` that trades live is driven by a simulated clock against the
point-in-time provider, so a bug in the strategy shows up identically in both.
The most common way backtests mislead is not a subtle statistical error — it is
that the backtest and the live system are two different programs, and only one
of them was ever tested.

What this can and cannot tell you:

  CAN   whether the decision logic produces positive expectancy on data it has
        not been fitted to, net of modelled costs, across several periods.

  CANNOT tell you it will work. Historical data contains the regime that
        happened, not the regimes that could have. Every backtest is a sample
        of one path through a world that ran once.

The result carries an `ambiguous_bars` count — bars where stop and target both
sat inside the range and the ordering had to be assumed. If that number is a
large fraction of trades, the headline figure is mostly an artefact of the
assumption rather than a measurement, and should be read as a range across
orderings instead of a number.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Optional, Sequence

from qd.clock import CALENDAR, MarketCalendar, Phase, SimClock
from qd.config import Mode, Settings
from qd.engine import Engine
from qd.journal import Journal
from qd.portfolio import ClosedTrade, Portfolio
from qd.providers.base import Providers
from qd.providers.replay import ReplayDataset, ReplayProvider
from qd.providers.sim import Ordering, SimBroker
from qd.types import Bar, Side, ensure_utc

logger = logging.getLogger(__name__)


@dataclass
class ReplayResult:
    trades: list[ClosedTrade] = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    cycles: int = 0
    ambiguous_bars: int = 0
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    cost_mult: float = 1.0
    ordering: str = "worst"
    blocked: dict[str, int] = field(default_factory=dict)

    # ── metrics ──────────────────────────────────────────────────────────────

    @property
    def count(self) -> int:
        return len(self.trades)

    def expectancy_r(self) -> float:
        """Mean R per trade — the only scale on which a $12 stock and a $600
        one are comparable."""
        if not self.trades:
            return 0.0
        return sum(t.r_multiple for t in self.trades) / len(self.trades)

    def profit_factor(self) -> float:
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = abs(sum(t.pnl for t in self.trades if t.pnl <= 0))
        if losses <= 0:
            return float("inf") if gains > 0 else 0.0
        return gains / losses

    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return sum(1 for t in self.trades if t.pnl > 0) / len(self.trades)

    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    def max_drawdown_pct(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0][1]
        worst = 0.0
        for _, eq in self.equity_curve:
            peak = max(peak, eq)
            if peak > 0:
                worst = max(worst, (peak - eq) / peak * 100.0)
        return worst

    def ambiguity_ratio(self) -> float:
        """Share of trades decided by the stop-versus-target assumption."""
        return self.ambiguous_bars / self.count if self.count else 0.0

    def summary(self) -> str:
        if not self.trades:
            return (
                f"no trades ({self.cycles} cycles, "
                f"{self.start:%Y-%m-%d} to {self.end:%Y-%m-%d})"
                if self.start else "no trades"
            )
        return (
            f"trades={self.count} expectancy={self.expectancy_r():+.4f}R "
            f"PF={self.profit_factor():.3f} win={self.win_rate():.1%} "
            f"pnl=${self.total_pnl():,.0f} maxDD={self.max_drawdown_pct():.1f}% "
            f"cost={self.cost_mult}x ordering={self.ordering} "
            f"ambiguous={self.ambiguity_ratio():.1%}"
        )


def run(
    settings: Settings,
    dataset: ReplayDataset,
    start: datetime,
    end: datetime,
    equity: float = 100_000.0,
    cost_mult: float = 1.0,
    ordering: Ordering = "worst",
    step: timedelta = timedelta(minutes=5),
    journal_path: str = "data/replay_journal.jsonl",
    cal: MarketCalendar = CALENDAR,
    progress: Optional[Callable[[datetime], None]] = None,
) -> ReplayResult:
    """Replay `dataset` between `start` and `end`."""
    start, end = ensure_utc(start), ensure_utc(end)
    clock = SimClock(start)

    # strict=False: the engine legitimately asks for data "up to now" with an
    # open-ended end, and the provider clamps. Strict mode is for the tests,
    # where any request past the clock is a bug worth failing on.
    provider = ReplayProvider(dataset, clock, strict=False)
    broker = SimBroker(equity, settings.execution, cost_mult=cost_mult, ordering=ordering)

    portfolio = Portfolio(equity, settings.risk, settings.universe, cal)
    journal = Journal(journal_path)
    providers = Providers(
        market=provider, broker=broker, news=provider,
        earnings=provider, options=provider,
    )
    engine = Engine(settings, providers, portfolio, journal, clock, cal)

    result = ReplayResult(start=start, end=end, cost_mult=cost_mult, ordering=ordering)
    result.equity_curve.append((start, equity))

    now = start
    while now < end:
        clock.set(now)

        # Advance the broker's fills BEFORE the engine decides. A bar that has
        # just closed may have taken out a stop, and the engine must see that
        # position as closed rather than reasoning about a position the market
        # already took away.
        for sym, state in engine.states.items():
            bars = dataset.bars.get(sym, [])
            for bar in bars:
                if now - step < bar.known_at <= now:
                    for ev in broker.on_bar(sym, bar):
                        if ev.kind != "entry":
                            trade = portfolio.close(sym, ev.price, ev.ts, ev.kind)
                            if trade:
                                result.trades.append(trade)
                                journal.exit(trade)

        try:
            report = engine.cycle()
            result.cycles += 1
            if report.halted:
                result.blocked[report.halted] = result.blocked.get(report.halted, 0) + 1
        except Exception:
            logger.exception("cycle failed at %s", now)

        portfolio.equity = broker.equity
        result.equity_curve.append((now, broker.equity))

        if progress:
            progress(now)
        now += step

    # Flatten anything still open at the end so the sample is not padded with
    # unrealised gains that were never actually taken.
    for pos in list(portfolio.all()):
        st = engine.states.get(pos.symbol)
        mark = st.snapshot.last if st and st.snapshot else pos.entry_price
        broker.force_close(pos.symbol, mark, end, "replay_end")
        trade = portfolio.close(pos.symbol, mark, end, "replay_end")
        if trade:
            result.trades.append(trade)

    result.ambiguous_bars = broker.ambiguous_bars
    result.blocked.update(journal.blocked_reasons())
    return result


def walk_forward(
    settings: Settings,
    dataset: ReplayDataset,
    start: datetime,
    end: datetime,
    folds: int = 4,
    **kwargs,
) -> list[ReplayResult]:
    """Split the period into consecutive folds and replay each.

    Consecutive rather than random: shuffling time destroys the autocorrelation
    that makes markets markets, and a random split lets a fold be "tested" on a
    period whose neighbours it was fitted to. Consecutive folds also answer the
    question that actually matters — does this survive a regime change, or does
    the whole result live in one lucky stretch?
    """
    start, end = ensure_utc(start), ensure_utc(end)
    span = (end - start) / folds
    out: list[ReplayResult] = []
    for i in range(folds):
        f_start = start + span * i
        f_end = start + span * (i + 1)
        logger.info("fold %d/%d: %s -> %s", i + 1, folds, f_start.date(), f_end.date())
        out.append(run(settings, dataset, f_start, f_end, **kwargs))
    return out


__all__ = ["ReplayResult", "run", "walk_forward"]
