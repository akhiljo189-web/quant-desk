"""
qd.engine — the loop that ties everything together.

One cycle:

    1. roll the trading day if the calendar moved
    2. refresh market data for every symbol in the universe
    3. gather evidence from all four channels
    4. manage OPEN positions first — exits before entries, always
    5. assess each candidate, size it, submit it
    6. journal everything, including the refusals

Two ordering decisions matter.

EXITS BEFORE ENTRIES. Capital and risk budget freed by a close should be
available to the same cycle, and more importantly a stop that needs moving must
not wait behind a scan of forty symbols. Risk reduction always outranks risk
addition.

THE WATCHDOG RUNS FIRST. If the data is stale, the loop halts entries before
doing anything else. A system trading on a frozen picture of the market is in
the one state where its confidence is entirely uncorrelated with reality, and
it has no way to notice from the inside — the numbers all still compute.

The engine holds no vendor knowledge. It is handed a `Providers` bundle and
cannot tell live from replay, which is what makes the backtest a test of this
code rather than of a parallel implementation that resembles it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from qd.clock import CALENDAR, Clock, LiveClock, MarketCalendar, Phase
from qd.config import Mode, Settings
from qd.context import ContextState, MarketContext, Regime, classify as classify_regime
from qd.features import earnings as earnings_ch
from qd.features import market as market_ch
from qd.features import news as news_ch
from qd.features import optionsflow as flow_ch
from qd.features.market import BarSeries, MarketSnapshot
from qd.journal import Journal
from qd.portfolio import Portfolio
from qd.providers.base import Providers
from qd.risk import size as size_trade
from qd.strategy import assess, build_intent
from qd.types import (
    Bar, EarningsEvent, Evidence, Intent, NewsItem, Order, Position, Side,
    ensure_utc,
)

logger = logging.getLogger(__name__)


@dataclass
class SymbolState:
    """Per-symbol working set, carried across cycles."""
    symbol: str
    intraday: BarSeries
    daily: BarSeries
    snapshot: Optional[MarketSnapshot] = None
    evidence: list[Evidence] = field(default_factory=list)
    last_refresh: Optional[datetime] = None

    def live_evidence(self, now: datetime) -> list[Evidence]:
        return [e for e in self.evidence if e.is_live(now)]

    def prune(self, now: datetime) -> None:
        self.evidence = self.live_evidence(now)
        self.intraday.trim(600)
        self.daily.trim(120)


@dataclass
class CycleReport:
    now: datetime
    phase: Phase
    assessed: int = 0
    intents: int = 0
    submitted: int = 0
    exits: int = 0
    halted: str = ""
    errors: list[str] = field(default_factory=list)

    def line(self) -> str:
        bits = [
            f"{self.now:%Y-%m-%d %H:%M:%S}", self.phase.value,
            f"assessed={self.assessed}", f"intents={self.intents}",
            f"orders={self.submitted}", f"exits={self.exits}",
        ]
        if self.halted:
            bits.append(f"HALTED({self.halted})")
        if self.errors:
            bits.append(f"errors={len(self.errors)}")
        return " ".join(bits)


class Engine:
    def __init__(
        self,
        settings: Settings,
        providers: Providers,
        portfolio: Portfolio,
        journal: Journal,
        clock: Clock = None,
        cal: MarketCalendar = CALENDAR,
    ) -> None:
        self.s = settings
        self.p = providers
        self.portfolio = portfolio
        self.journal = journal
        self.clock = clock or LiveClock()
        self.cal = cal

        self.states: dict[str, SymbolState] = {
            sym: SymbolState(sym, BarSeries(sym), BarSeries(sym))
            for sym in settings.universe.symbols
        }
        self.novelty = news_ch.NoveltyTracker(settings.news.novelty_window)
        self.flow_baseline = flow_ch.FlowBaseline()
        self._earnings: list[EarningsEvent] = []
        self._earnings_fetched: Optional[datetime] = None
        self._news_cursor: Optional[datetime] = None

        # Index bars for the market-wide regime read, kept separately from the
        # tradeable universe — SPY is context, never a candidate.
        self._market_bars = BarSeries(settings.context.market_symbol)
        self._market_context: Optional[ContextState] = None
        # Regime is derived from DAILY bars, so it can only change when one
        # closes. Caching per trading day turns a per-cycle recomputation over
        # a year of history into one per symbol per day.
        self._context_cache: dict[tuple[str, str], ContextState] = {}

    # ── data ─────────────────────────────────────────────────────────────────

    def refresh_market(self, symbol: str, now: datetime) -> None:
        st = self.states[symbol]
        lookback = timedelta(minutes=self.s.market.bar_minutes * 200)
        try:
            for b in self.p.market.bars(
                symbol, now - lookback, now, self.s.market.bar_minutes
            ):
                st.intraday.append(b)
            if not len(st.daily) or (
                st.last_refresh is None
                or now - st.last_refresh > timedelta(hours=6)
            ):
                for b in self.p.market.daily_bars(symbol, now - timedelta(days=120), now):
                    st.daily.append(b)
        except Exception as exc:
            logger.warning("%s: market refresh failed: %s", symbol, exc)
            self.journal.error("market refresh failed", symbol=symbol, detail=str(exc))
            return

        st.snapshot = market_ch.build_snapshot(
            symbol, st.intraday, st.daily, now, self.s.market, self.cal
        )
        st.last_refresh = now

    def _classify_cached(
        self, symbol: str, bars, now: datetime
    ) -> ContextState:
        """Classify once per symbol per trading day."""
        key = (symbol.upper(), self.cal.trading_day_key(now))
        hit = self._context_cache.get(key)
        if hit is not None:
            return hit
        state = classify_regime(symbol, bars, now)
        # Bound the cache: two entries per symbol is ample, and an unbounded
        # dict in a process that runs for months is a slow leak.
        if len(self._context_cache) > len(self.states) * 4 + 8:
            self._context_cache.clear()
        self._context_cache[key] = state
        return state

    def refresh_context(self, now: datetime) -> None:
        """Refresh index bars and classify the market-wide regime."""
        if not self.s.context.enabled:
            return
        sym = self.s.context.market_symbol
        try:
            for b in self.p.market.daily_bars(sym, now - timedelta(days=500), now):
                self._market_bars.append(b)
        except Exception as exc:
            logger.warning("%s: context refresh failed: %s", sym, exc)
            return
        self._market_context = self._classify_cached(sym, list(self._market_bars), now)

    def context_for(self, symbol: str, now: datetime) -> Optional[MarketContext]:
        if not self.s.context.enabled:
            return None
        st = self.states.get(symbol)
        if st is None:
            return None
        symbol_ctx = self._classify_cached(symbol, list(st.daily), now)
        market_ctx = self._market_context or self._classify_cached(
            self.s.context.market_symbol, list(self._market_bars), now
        )
        return MarketContext(market=market_ctx, symbol=symbol_ctx)

    def refresh_news(self, now: datetime) -> None:
        if self.p.news is None:
            return
        since = self._news_cursor or (now - self.s.news.ttl)
        try:
            items = self.p.news.news(list(self.states), since, now)
        except Exception as exc:
            logger.warning("news refresh failed: %s", exc)
            self.journal.error("news refresh failed", detail=str(exc))
            return

        for sym, st in self.states.items():
            st.evidence.extend(
                news_ch.evaluate(sym, items, now, self.s.news, self.novelty)
            )
        self._news_cursor = now

    def refresh_earnings(self, now: datetime) -> None:
        if self.p.earnings is None:
            return
        # The schedule changes slowly; refetching it every cycle wastes quota
        # that the options tape needs.
        if self._earnings_fetched and now - self._earnings_fetched < timedelta(hours=4):
            return
        try:
            self._earnings = self.p.earnings.earnings(
                list(self.states), now - timedelta(days=7), now + timedelta(days=21)
            )
            self._earnings_fetched = now
        except Exception as exc:
            logger.warning("earnings refresh failed: %s", exc)
            self.journal.error("earnings refresh failed", detail=str(exc))

    def refresh_flow(self, symbol: str, now: datetime) -> None:
        if self.p.options is None:
            return
        st = self.states[symbol]
        try:
            trades = self.p.options.option_trades(
                symbol, now - self.s.options.window, now
            )
        except Exception as exc:
            logger.warning("%s: options tape failed: %s", symbol, exc)
            return
        if not trades:
            return
        st.evidence.extend(
            flow_ch.evaluate(symbol, trades, now, self.s.options, self.flow_baseline)
        )

    def refresh_evidence(self, symbol: str, now: datetime) -> None:
        st = self.states[symbol]
        if st.snapshot is not None:
            st.evidence.extend(
                market_ch.evaluate(st.snapshot, st.intraday, self.s.market, self.cal)
            )
        reaction = self._earnings_reaction(symbol, now)
        st.evidence.extend(
            earnings_ch.evaluate(symbol, self._earnings, now, self.s.earnings, reaction)
        )

    def _earnings_reaction(self, symbol: str, now: datetime) -> Optional[float]:
        """Percentage move since the last earnings release, if there was one.

        This is the market's verdict on the whole report, which the earnings
        channel weighs against the headline EPS surprise.
        """
        st = self.states[symbol]
        if st.snapshot is None or not st.snapshot.last:
            return None
        for ev in self._earnings:
            if ev.symbol.upper() != symbol.upper() or not ev.has_actuals_at(now):
                continue
            released = ev.actuals_known_at()
            if released is None or now - released > self.s.earnings.pead_window:
                continue
            before = [b for b in st.daily.visible_at(now) if b.end <= released]
            if not before:
                continue
            base = before[-1].close
            if base > 0:
                return (st.snapshot.last - base) / base * 100.0
        return None

    # ── watchdog ─────────────────────────────────────────────────────────────

    def staleness(self, now: datetime) -> Optional[str]:
        """Whether the market data has stopped arriving.

        Only meaningful during regular hours: outside them the absence of new
        bars is the market being shut, not a broken feed.
        """
        if self.cal.phase(now) is not Phase.REGULAR:
            return None

        # Just after the open no bar has closed yet, so the newest one is
        # yesterday's and legitimately hours old. Flagging that as a broken
        # feed would fire an alarm at 09:30 every single day, and an alarm that
        # cries wolf daily is one nobody reads on the morning it is real.
        since_open = self.cal.minutes_since_open(now)
        if since_open is not None and since_open < self.s.market.bar_minutes + 1:
            return None

        ages: list[tuple[str, timedelta]] = []
        for sym, st in self.states.items():
            if st.snapshot is None:
                continue
            age = st.snapshot.stale_by(now)
            if age is not None:
                ages.append((sym, age))
        if not ages:
            return "no market data at all"
        worst_sym, worst = max(ages, key=lambda kv: kv[1])
        if worst > self.s.risk.max_bar_age:
            fresh = sum(1 for _, a in ages if a <= self.s.risk.max_bar_age)
            if fresh == 0:
                return (
                    f"every symbol stale (worst {worst_sym} "
                    f"{worst.total_seconds():.0f}s)"
                )
        return None

    # ── position management ──────────────────────────────────────────────────

    def manage_positions(self, now: datetime, report: CycleReport) -> None:
        """Exits, stop moves and time stops. Runs before any new entry."""
        cfg = self.s.strategy
        for pos in self.portfolio.all():
            st = self.states.get(pos.symbol)
            if st is None or st.snapshot is None or not st.snapshot.last:
                continue
            mark = st.snapshot.last
            r = pos.r_multiple(mark)
            held = now - pos.opened_at

            # Break even after a partial. Once some profit is banked, the
            # remainder should not be allowed to become a loss.
            if (
                cfg.breakeven_after_partial and pos.partial_taken
                and not pos.stop_moved_to_breakeven
            ):
                if self.p.broker.replace_stop(pos.symbol, pos.entry_price):
                    pos.stop_moved_to_breakeven = True
                    pos.stop_price = pos.entry_price
                    self.journal.event("stop to breakeven", symbol=pos.symbol)

            # Partial take-profit.
            if not pos.partial_taken and r >= cfg.partial_take_r:
                qty = int(pos.quantity * cfg.partial_fraction)
                if qty >= 1:
                    self.p.broker.close_position(pos.symbol, qty)
                    pos.partial_taken = True
                    pos.quantity -= qty
                    self.journal.event(
                        "partial take", symbol=pos.symbol, quantity=qty,
                        r_multiple=round(r, 3),
                    )
                    report.exits += 1

            # Drift-window exit, unconditional. The hypothesis is that the
            # earnings event causes a drift with a lifespan; past that window
            # the position is no longer held because of the event, and keeping
            # it is an unregistered momentum bet wearing the hypothesis's
            # clothes. Winners are closed here too — that is the point.
            if held >= cfg.max_hold:
                self.p.broker.close_position(pos.symbol)
                trade = self.portfolio.close(pos.symbol, mark, now, "drift_window_over")
                if trade:
                    self.journal.exit(trade)
                report.exits += 1
                continue

            # Early cut. A trade flat by day three has spent most of its drift
            # window not drifting — the thesis is failing in the one interval
            # where it was supposed to work, and the slot and risk budget are
            # worth more than the residual hope.
            if held >= cfg.time_stop and abs(r) < cfg.time_stop_r_threshold:
                self.p.broker.close_position(pos.symbol)
                trade = self.portfolio.close(pos.symbol, mark, now, "time_stop")
                if trade:
                    self.journal.exit(trade)
                report.exits += 1
                continue

            # Force flat before the close when the position may not be held out.
            if self._must_flatten(pos, now):
                self.p.broker.close_position(pos.symbol)
                trade = self.portfolio.close(pos.symbol, mark, now, "eod_flat")
                if trade:
                    self.journal.exit(trade)
                report.exits += 1

    def _must_flatten(self, pos: Position, now: datetime) -> bool:
        mins = self.cal.minutes_to_close(now)
        if mins is None or mins > 10:
            return False
        if not self.s.risk.allow_overnight:
            return True
        # An earnings report tonight is the clearest case: the stop does not
        # survive the gap, so the position must not survive the session.
        if self.s.risk.flatten_before_earnings:
            nxt = earnings_ch.next_event(pos.symbol, self._earnings, now)
            if nxt is not None:
                release = earnings_ch.expected_release_time(nxt, self.cal)
                if release - now < timedelta(hours=20):
                    return True
        from qd.risk import overnight_check
        return pos.symbol in overnight_check(self.portfolio, self.s.risk, now, self.cal)

    # ── entries ──────────────────────────────────────────────────────────────

    def consider(self, symbol: str, now: datetime, report: CycleReport) -> None:
        st = self.states[symbol]
        if st.snapshot is None:
            return

        ok, why = market_ch.is_tradeable(st.snapshot, self.s.universe)
        if not ok:
            return

        a = assess(
            symbol, st.live_evidence(now), st.snapshot, now, self.s.strategy,
            self.cal, self.context_for(symbol, now), self.s.context,
        )
        report.assessed += 1

        if not a.would_trade:
            self.journal.assessment(a, taken=False)
            return

        intent = build_intent(a, st.snapshot, self.s.strategy, self.s.risk)
        if intent is None:
            self.journal.assessment(a, taken=False, blocked="reward:risk below minimum")
            return
        report.intents += 1

        bo = earnings_ch.blackout(symbol, self._earnings, now, self.s.earnings, self.cal)
        decision = size_trade(
            intent, self.portfolio, self.s.risk, self.s.universe, now,
            earnings_blackout=bo.reason if bo.active else None,
            data_stale=report.halted or None,
            cal=self.cal,
        )
        self.journal.risk_decision(intent, decision)

        if not decision.approved:
            self.journal.assessment(a, taken=False, blocked=decision.reason)
            return

        limit = self._limit_price(intent)
        order = Order(
            symbol=intent.symbol, side=intent.side, quantity=decision.quantity,
            stop_price=round(intent.stop_price, 2),
            target_price=round(intent.target_price, 2),
            client_order_id=intent.idempotency_key(),
            intent=intent, limit_price=limit,
        )

        try:
            bo_order = self.p.broker.submit(order)
        except Exception as exc:
            logger.error("%s: order rejected: %s", symbol, exc)
            self.journal.error("order rejected", symbol=symbol, detail=str(exc))
            report.errors.append(f"{symbol}: {exc}")
            return

        self.portfolio.open(Position(
            symbol=intent.symbol, side=intent.side, quantity=decision.quantity,
            entry_price=intent.reference_price, stop_price=intent.stop_price,
            target_price=intent.target_price, opened_at=now, intent=intent,
        ))
        self.journal.order(intent, bo_order, decision.quantity)
        self.journal.assessment(a, taken=True)
        report.submitted += 1

    def _limit_price(self, intent: Intent) -> float:
        """Marketable limit: crosses the spread but caps the damage.

        A market order accepts any price the book offers. This one accepts a
        bad fill and refuses an absurd one.
        """
        off = intent.reference_price * self.s.execution.limit_offset_bps / 10_000.0
        px = intent.reference_price + (off if intent.side is Side.BUY else -off)
        return round(px, 2)

    # ── cycle ────────────────────────────────────────────────────────────────

    def cycle(self) -> CycleReport:
        now = ensure_utc(self.clock.now())
        report = CycleReport(now=now, phase=self.cal.phase(now))

        if self.portfolio.roll_day(now):
            self.novelty.prune(now)
            self._news_cursor = None

        for st in self.states.values():
            st.prune(now)

        for sym in self.states:
            self.refresh_market(sym, now)
        self.refresh_context(now)
        self.refresh_earnings(now)
        self.refresh_news(now)
        for sym in self.states:
            self.refresh_evidence(sym, now)
            self.refresh_flow(sym, now)

        stale = self.staleness(now)
        if stale:
            report.halted = stale
            logger.warning("STALE DATA: %s — entries halted", stale)
            self.journal.event("stale data", detail=stale)

        # Exits first, unconditionally: they run even while entries are halted,
        # because a halt is a reason to reduce risk, never a reason to sit on it.
        self.manage_positions(now, report)

        breaker = self.portfolio.breaker()
        if breaker.active:
            report.halted = report.halted or breaker.reason
            return report

        if report.phase is Phase.CLOSED:
            return report

        for sym in self.states:
            if self.portfolio.has(sym):
                continue
            try:
                self.consider(sym, now, report)
            except Exception as exc:
                logger.exception("%s: consider failed", sym)
                report.errors.append(f"{sym}: {exc}")
                self.journal.error("consider failed", symbol=sym, detail=str(exc))

        return report

    def startup(self) -> None:
        """Reconcile with the broker before the first cycle."""
        logger.info("engine starting: %s", self.s.describe())
        logger.info("providers: %s", self.p.describe())
        self.journal.event("startup", mode=self.s.mode.value, config=self.s.describe())

        if self.s.execution.reconcile_on_start and hasattr(self.p.broker, "reconcile"):
            try:
                positions, naked = self.p.broker.reconcile()
                for pos in positions:
                    if not self.portfolio.has(pos.symbol):
                        self.portfolio.open(pos)
                if naked:
                    # Unprotected positions are the worst possible startup
                    # state, so the response is to stop trading and say so
                    # loudly rather than to trade around them.
                    self.portfolio.halt(
                        f"positions without a protective stop: {', '.join(naked)}"
                    )
                    self.journal.error("naked positions on startup", symbols=naked)
            except Exception as exc:
                logger.error("reconcile failed: %s", exc)
                self.journal.error("reconcile failed", detail=str(exc))


__all__ = ["Engine", "SymbolState", "CycleReport"]
