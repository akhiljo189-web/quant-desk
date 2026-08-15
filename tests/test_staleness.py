"""
The staleness watchdog against a delayed feed.

A 15-minute delayed data tier is the right purchase for this strategy — PEAD is
a multi-day drift and pays nothing for speed. But a watchdog that does not know
about the delay reads the vendor's normal behaviour as a dead feed and halts
every entry, permanently, while looking exactly like a signal that never fires.

That failure costs weeks before anyone diagnoses it, so it gets its own tests:
the delayed feed must NOT halt, and a genuinely dead one must still halt.
"""

from __future__ import annotations

import dataclasses
import unittest
from datetime import datetime, timedelta

from qd.config import Mode, Settings
from qd.engine import Engine, SymbolState
from qd.features.market import BarSeries, MarketSnapshot
from qd.journal import Journal
from qd.portfolio import Portfolio
from qd.providers.base import Providers
from qd.types import UTC

# Tuesday 2026-03-10, 16:00 UTC = 12:00 ET — mid-session, well past the open.
NOW = datetime(2026, 3, 10, 16, 0, tzinfo=UTC)


class _NullProvider:
    """Satisfies the Providers bundle without doing anything."""

    def bars(self, *a, **k): return []
    def daily_bars(self, *a, **k): return []
    def quote(self, *a, **k): return None
    def account(self): raise NotImplementedError
    def positions(self): return []
    def open_orders(self): return []
    def submit(self, order): raise NotImplementedError
    def cancel(self, oid): return False
    def close_position(self, sym, qty=None): return None
    def replace_stop(self, sym, px): return False


def engine_with(feed_delay: timedelta, bar_age: timedelta,
                bar_minutes: int = 5) -> Engine:
    """An engine whose only symbol has a snapshot `bar_age` old."""
    s = Settings.load(Mode.REPLAY)
    s = dataclasses.replace(
        s,
        universe=dataclasses.replace(s.universe, symbols=("AAPL",)),
        providers=dataclasses.replace(s.providers, feed_delay=feed_delay),
        market=dataclasses.replace(s.market, bar_minutes=bar_minutes),
    )
    null = _NullProvider()
    eng = Engine(
        s, Providers(market=null, broker=null),
        Portfolio(100_000, s.risk, s.universe),
        Journal("data/test_staleness.jsonl"),
    )
    st = SymbolState("AAPL", BarSeries("AAPL"), BarSeries("AAPL"))
    st.snapshot = MarketSnapshot(
        symbol="AAPL", now=NOW, last=100.0, atr=2.0, atr_pct=2.0, vwap=100.0,
        ema_fast=100.0, ema_slow=100.0, rvol=1.0, gap_pct=0.0, prev_close=100.0,
        session_high=101.0, session_low=99.0, adv_dollar=50_000_000.0,
        bar_count=100, last_bar_end=NOW - bar_age,
    )
    eng.states = {"AAPL": st}
    return eng


class DelayedFeedTest(unittest.TestCase):
    def test_delayed_feed_does_not_halt(self):
        """THE regression test. A 15-minute delayed tier is normal operation,
        not a broken feed — without this the paper run places zero trades and
        the cause is invisible."""
        eng = engine_with(timedelta(minutes=15), timedelta(minutes=16))
        self.assertIsNone(eng.staleness(NOW))

    def test_realtime_feed_halts_sooner_than_a_delayed_one(self):
        """Sensitivity is relative, and that is the property worth pinning.

        An absolute threshold here would be re-derived every time the tolerance
        formula changes — as it has twice. What must stay true is that a
        real-time feed is held to a tighter standard than a delayed one, so an
        age the delayed tier tolerates halts the real-time tier.
        """
        age = timedelta(minutes=12)                 # 5-min bars, no delay: > 5+0+5
        self.assertIsNotNone(engine_with(timedelta(0), age).staleness(NOW))
        self.assertIsNone(engine_with(timedelta(minutes=15), age).staleness(NOW))

    def test_genuinely_dead_delayed_feed_still_halts(self):
        """Tolerance is bounded, not infinite. A feed that stopped two hours
        ago is dead on any tier."""
        eng = engine_with(timedelta(minutes=15), timedelta(minutes=120))
        reason = eng.staleness(NOW)
        self.assertIsNotNone(reason)
        self.assertIn("stale", reason)

    def test_tolerance_is_bar_interval_plus_delay_plus_max_bar_age(self):
        delay, s = timedelta(minutes=15), Settings.load(Mode.REPLAY)
        # 5-minute bars: 5 + 15 + 5 = 25 minutes
        tolerance = timedelta(minutes=5) + delay + s.risk.max_bar_age
        self.assertIsNone(engine_with(delay, tolerance - timedelta(minutes=1)).staleness(NOW))
        self.assertIsNotNone(engine_with(delay, tolerance + timedelta(minutes=1)).staleness(NOW))

    def test_hourly_bars_are_not_stale_at_fifty_nine_minutes(self):
        """THE regression test for the second time this watchdog halted
        everything.

        A bar's age is measured from its CLOSE, so on 60-minute bars the newest
        one is up to an hour old the instant before the next closes. That is the
        sampling interval, not a dead feed. Reading it as staleness halted every
        entry across a four-year backtest — a run that produces no trades and no
        error, and looks exactly like a strategy that never triggers.
        """
        eng = engine_with(timedelta(0), timedelta(minutes=59), bar_minutes=60)
        self.assertIsNone(eng.staleness(NOW))

    def test_hourly_bars_still_halt_when_the_feed_really_stops(self):
        """Scaling the tolerance to the interval must not disable the check."""
        eng = engine_with(timedelta(0), timedelta(minutes=90), bar_minutes=60)
        self.assertIsNotNone(eng.staleness(NOW))

    def test_daily_bars_scale_too(self):
        eng = engine_with(timedelta(0), timedelta(hours=20), bar_minutes=1440)
        self.assertIsNone(eng.staleness(NOW))

    def test_no_data_at_all_is_always_stale(self):
        eng = engine_with(timedelta(minutes=15), timedelta(minutes=1))
        eng.states["AAPL"].snapshot = None
        self.assertEqual(eng.staleness(NOW), "no market data at all")

    def test_outside_regular_hours_never_stale(self):
        """Absence of bars overnight is the market being shut."""
        eng = engine_with(timedelta(minutes=15), timedelta(hours=12))
        self.assertIsNone(eng.staleness(datetime(2026, 3, 10, 3, 0, tzinfo=UTC)))

    def test_open_grace_period_covers_the_feed_delay(self):
        """Just after the open, a delayed feed has nothing from today at all.
        The grace window must span the delay or the watchdog fires every
        morning — and a daily false alarm is one nobody reads on the day it is
        real."""
        eng = engine_with(timedelta(minutes=15), timedelta(hours=18))
        just_open = datetime(2026, 3, 10, 13, 40, tzinfo=UTC)   # 09:40 ET
        self.assertIsNone(eng.staleness(just_open))


if __name__ == "__main__":
    unittest.main()
