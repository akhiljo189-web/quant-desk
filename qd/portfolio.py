"""
qd.portfolio — open positions, exposure, realised P&L and circuit breakers.

The book of record for what the system currently owns and what it has lost
today. Everything here is deliberately boring and explicit, because this is the
module that decides when to stop.

Three things it tracks that are easy to get wrong:

  ROLLING P&L      Realised only. Unrealised swings are noise and a breaker
                   that trips on them fires during the ordinary wobble of a
                   position that is working.

  DAY TRADES       The PDT rule counts round trips per rolling five business
                   days, and breaching it under $25k freezes the account for
                   ninety days. That ends the experiment regardless of results,
                   so it is enforced as a hard constraint rather than a
                   preference.

  OPEN RISK        The sum of every position's distance to its stop. Position
                   count and gross notional both understate what a correlated
                   move costs; this is the number that answers "if today goes
                   badly, how badly".
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Optional, Sequence

from qd.clock import CALENDAR, MarketCalendar
from qd.config import RiskConfig, UniverseConfig
from qd.types import Fill, Position, Side, ensure_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    exit_price: float
    opened_at: datetime
    closed_at: datetime
    pnl: float
    r_multiple: float
    reason: str = ""
    sector: str = "unknown"

    @property
    def hold_time(self) -> timedelta:
        return self.closed_at - self.opened_at


@dataclass(frozen=True)
class BreakerState:
    active: bool
    reason: str = ""
    until: str = ""       # "day" | "week" | ""


class Portfolio:
    """Live state of the book."""

    def __init__(
        self,
        equity: float,
        risk: RiskConfig,
        universe: UniverseConfig,
        cal: MarketCalendar = CALENDAR,
    ) -> None:
        self.risk = risk
        self.universe = universe
        self.cal = cal

        self.equity = equity
        self.day_start_equity = equity
        self.week_start_equity = equity

        self._positions: dict[str, Position] = {}
        self.closed: list[ClosedTrade] = []

        self.daily_realized = 0.0
        self.weekly_realized = 0.0
        self.daily_loss_streak = 0
        self.weekly_loss_streak = 0
        self.opens_today = 0

        self._current_day: Optional[str] = None
        # (trading_day, symbol) pairs that were opened AND closed the same day.
        self._day_trades: deque[tuple[str, str]] = deque(maxlen=64)
        self._halt_reason: str = ""

    # ── positions ────────────────────────────────────────────────────────────

    def open(self, pos: Position) -> None:
        if pos.symbol in self._positions:
            raise ValueError(f"{pos.symbol}: already open")
        pos.sector = self.universe.sector_of(pos.symbol)
        pos.opened_session = self.cal.trading_day_key(pos.opened_at)
        self._positions[pos.symbol] = pos
        self.opens_today += 1
        logger.info(
            "OPEN %s %s %.4f @ %.4f stop=%.4f risk=$%.2f sector=%s",
            pos.symbol, pos.side.value, pos.quantity, pos.entry_price,
            pos.stop_price, pos.open_risk, pos.sector,
        )

    def close(
        self, symbol: str, exit_price: float, when: datetime, reason: str = ""
    ) -> Optional[ClosedTrade]:
        pos = self._positions.pop(symbol, None)
        if pos is None:
            return None
        when = ensure_utc(when)
        pnl = pos.unrealized(exit_price)
        trade = ClosedTrade(
            symbol=symbol, side=pos.side, quantity=pos.quantity,
            entry_price=pos.entry_price, exit_price=exit_price,
            opened_at=pos.opened_at, closed_at=when, pnl=pnl,
            r_multiple=pos.r_multiple(exit_price), reason=reason, sector=pos.sector,
        )
        self.closed.append(trade)
        self._record_pnl(pnl)

        # Same-session round trip — a day trade for PDT purposes.
        if pos.opened_session and pos.opened_session == self.cal.trading_day_key(when):
            self._day_trades.append((pos.opened_session, symbol))

        logger.info(
            "CLOSE %s @ %.4f pnl=$%.2f (%.2fR) reason=%s",
            symbol, exit_price, pnl, trade.r_multiple, reason,
        )
        return trade

    def get(self, symbol: str) -> Optional[Position]:
        return self._positions.get(symbol)

    def has(self, symbol: str) -> bool:
        return symbol.upper() in self._positions

    def all(self) -> list[Position]:
        return list(self._positions.values())

    def __len__(self) -> int:
        return len(self._positions)

    # ── exposure ─────────────────────────────────────────────────────────────

    def gross_notional(self) -> float:
        return sum(p.notional for p in self._positions.values())

    def net_notional(self) -> float:
        return sum(p.notional * p.side.sign for p in self._positions.values())

    def sector_notional(self, sector: str) -> float:
        return sum(p.notional for p in self._positions.values() if p.sector == sector)

    def sector_count(self, sector: str) -> int:
        return sum(1 for p in self._positions.values() if p.sector == sector)

    def total_open_risk(self) -> float:
        """Cash at risk across the book if every stop fills at its price."""
        return sum(p.open_risk for p in self._positions.values())

    def open_risk_pct(self) -> float:
        return (self.total_open_risk() / self.equity * 100.0) if self.equity > 0 else 0.0

    def exposure_summary(self) -> dict[str, float]:
        eq = self.equity or 1.0
        return {
            "gross": self.gross_notional() / eq,
            "net": self.net_notional() / eq,
            "open_risk_pct": self.open_risk_pct(),
            "positions": float(len(self._positions)),
        }

    # ── P&L and breakers ─────────────────────────────────────────────────────

    def _record_pnl(self, pnl: float) -> None:
        self.daily_realized += pnl
        self.weekly_realized += pnl
        self.equity += pnl
        if pnl < 0:
            self.daily_loss_streak += 1
            self.weekly_loss_streak += 1
        else:
            self.daily_loss_streak = 0
            self.weekly_loss_streak = 0

    def daily_pnl_pct(self) -> float:
        if self.day_start_equity <= 0:
            return 0.0
        return self.daily_realized / self.day_start_equity * 100.0

    def weekly_pnl_pct(self) -> float:
        if self.week_start_equity <= 0:
            return 0.0
        return self.weekly_realized / self.week_start_equity * 100.0

    def breaker(self) -> BreakerState:
        """Whether trading is halted, and why.

        Loss-percentage limits and loss streaks are separate tests on purpose.
        The percentage catches one bad position sized correctly; the streak
        catches a signal that has stopped working — six small losses in a row
        is information even when the total is modest.
        """
        if self._halt_reason:
            return BreakerState(True, self._halt_reason, "day")
        r = self.risk
        if self.daily_pnl_pct() <= -r.daily_loss_stop_pct:
            return BreakerState(
                True, f"daily loss {self.daily_pnl_pct():.2f}% <= -{r.daily_loss_stop_pct}%", "day"
            )
        if self.weekly_pnl_pct() <= -r.weekly_loss_stop_pct:
            return BreakerState(
                True, f"weekly loss {self.weekly_pnl_pct():.2f}% <= -{r.weekly_loss_stop_pct}%", "week"
            )
        if self.daily_loss_streak >= r.daily_loss_streak:
            return BreakerState(True, f"{self.daily_loss_streak} consecutive losses today", "day")
        if self.weekly_loss_streak >= r.weekly_loss_streak:
            return BreakerState(True, f"{self.weekly_loss_streak} consecutive losses this week", "week")
        return BreakerState(False)

    def halt(self, reason: str) -> None:
        """Manual or watchdog-triggered stop, cleared at the next day roll."""
        self._halt_reason = reason
        logger.warning("HALT: %s", reason)

    def resume(self) -> None:
        self._halt_reason = ""

    # ── PDT ──────────────────────────────────────────────────────────────────

    def day_trades_in_window(self, now: datetime, days: int = 5) -> int:
        """Round trips within the trailing five BUSINESS days.

        Counted on business days rather than calendar days — over a weekend the
        calendar version silently drops two sessions' worth of trades and lets
        the count reset early.
        """
        today = ensure_utc(now).date()
        window: list[date] = []
        d = today
        while len(window) < days:
            if self.cal.is_trading_day(d):
                window.append(d)
            d -= timedelta(days=1)
        keys = {dt.isoformat() for dt in window}
        return sum(1 for day, _ in self._day_trades if day in keys)

    def pdt_blocked(self, now: datetime) -> tuple[bool, str]:
        """Whether opening a position that might round-trip today is unsafe.

        The check is conservative: it looks at whether we are already AT the
        limit, because a position opened now may well need to be closed today,
        and discovering the breach at exit time is too late.
        """
        r = self.risk
        if not r.pdt_enforce or self.equity >= r.pdt_equity_threshold:
            return False, ""
        used = self.day_trades_in_window(now)
        if used >= r.pdt_max_day_trades:
            return True, (
                f"PDT: {used}/{r.pdt_max_day_trades} day trades used in the "
                f"rolling 5 sessions and equity ${self.equity:,.0f} < "
                f"${r.pdt_equity_threshold:,.0f}"
            )
        return False, ""

    # ── rolls ────────────────────────────────────────────────────────────────

    def roll_day(self, now: datetime, equity: Optional[float] = None) -> bool:
        """Advance the trading day if the calendar has moved on. Returns True
        when a roll happened."""
        key = self.cal.trading_day_key(now)
        if self._current_day == key:
            return False
        prev, self._current_day = self._current_day, key
        if equity is not None:
            self.equity = equity
        self.day_start_equity = self.equity
        self.daily_realized = 0.0
        self.daily_loss_streak = 0
        self.opens_today = 0
        self._halt_reason = ""
        if ensure_utc(now).weekday() == 0 or prev is None:
            self.week_start_equity = self.equity
            self.weekly_realized = 0.0
            self.weekly_loss_streak = 0
        logger.info("Day roll -> %s (equity $%.2f)", key, self.equity)
        return True

    # ── stats ────────────────────────────────────────────────────────────────

    def stats(self) -> dict[str, float]:
        if not self.closed:
            return {"trades": 0}
        wins = [t for t in self.closed if t.pnl > 0]
        losses = [t for t in self.closed if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        rs = [t.r_multiple for t in self.closed]
        return {
            "trades": len(self.closed),
            "win_rate": len(wins) / len(self.closed),
            "expectancy_r": sum(rs) / len(rs),
            "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else float("inf"),
            "total_pnl": sum(t.pnl for t in self.closed),
            "avg_win_r": (sum(t.r_multiple for t in wins) / len(wins)) if wins else 0.0,
            "avg_loss_r": (sum(t.r_multiple for t in losses) / len(losses)) if losses else 0.0,
        }


__all__ = ["Portfolio", "ClosedTrade", "BreakerState"]
