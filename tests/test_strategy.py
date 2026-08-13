"""
Strategy: confluence, conflict handling, and — critically — proof that the
system CAN trade.

A system that never trades passes every safety test trivially. The tests that
matter here come in pairs: one showing a gate blocks what it should, and one
showing the same gate lets a genuinely aligned setup through. Without the
second half of each pair, "no trades" and "correctly cautious" are
indistinguishable.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.config import RiskConfig, Settings, StrategyConfig
from qd.features.market import MarketSnapshot
from qd.strategy import aggregate, assess, build_intent, stop_for
from qd.types import Evidence, Side, Source, UTC

# A Tuesday, 15:00 UTC = 11:00 ET — mid-session, well clear of both edges.
NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def snapshot(price=100.0, atr=2.0, **kw) -> MarketSnapshot:
    defaults = dict(
        symbol="NVDA", now=NOW, last=price, atr=atr, atr_pct=atr / price * 100,
        vwap=price, ema_fast=price, ema_slow=price, rvol=1.5, gap_pct=0.0,
        prev_close=price, session_high=price * 1.01, session_low=price * 0.99,
        adv_dollar=500_000_000.0, bar_count=120, last_bar_end=NOW,
    )
    defaults.update(kw)
    return MarketSnapshot(**defaults)


def ev(source: Source, score: float, conf: float = 0.8, kind="test",
       age=timedelta(0), ttl=timedelta(hours=1)) -> Evidence:
    return Evidence(
        source=source, kind=kind, symbol="NVDA", score=score, confidence=conf,
        observed_at=NOW - age, ttl=ttl,
    )


class ConfluenceTest(unittest.TestCase):
    def setUp(self):
        self.s = Settings.load()

    def test_single_source_never_trades(self):
        """One loud channel is more likely to be that channel breaking than a
        real opportunity."""
        a = assess("NVDA", [ev(Source.NEWS, 0.95, 0.95)], snapshot(), NOW,
                   self.s.strategy)
        self.assertFalse(a.would_trade)
        self.assertIn("confluence", a.blocked)

    def test_two_agreeing_sources_can_trade(self):
        a = assess(
            "NVDA",
            [ev(Source.NEWS, 0.85, 0.9), ev(Source.OPTIONS_FLOW, 0.75, 0.85)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertTrue(a.would_trade, a.blocked)
        self.assertIs(a.direction, Side.BUY)
        self.assertGreaterEqual(a.agreeing_sources, 2)

    def test_bearish_confluence_goes_short(self):
        a = assess(
            "NVDA",
            [ev(Source.NEWS, -0.85, 0.9), ev(Source.OPTIONS_FLOW, -0.75, 0.85)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertTrue(a.would_trade, a.blocked)
        self.assertIs(a.direction, Side.SELL)

    def test_conflicting_channels_are_vetoed(self):
        a = assess(
            "NVDA",
            [ev(Source.NEWS, 0.9, 0.95), ev(Source.OPTIONS_FLOW, -0.9, 0.95),
             ev(Source.MARKET, 0.3, 0.6)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertFalse(a.would_trade)

    def test_a_shrug_does_not_count_as_agreement(self):
        """A 0.02 reading is not a confirming vote."""
        a = assess(
            "NVDA",
            [ev(Source.NEWS, 0.9, 0.9), ev(Source.OPTIONS_FLOW, 0.02, 0.9)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertFalse(a.would_trade)
        self.assertIn("confluence", a.blocked)

    def test_expired_evidence_is_ignored(self):
        a = assess(
            "NVDA",
            [ev(Source.NEWS, 0.9, 0.9, age=timedelta(hours=3), ttl=timedelta(hours=1)),
             ev(Source.OPTIONS_FLOW, 0.8, 0.9)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertFalse(a.would_trade)

    def test_many_weak_market_readings_do_not_outvote_one_strong_channel(self):
        """Averaging within a source stops a channel that happens to emit four
        readings from dominating one that emits a single stronger one."""
        many = [ev(Source.MARKET, 0.2, 0.5, kind=f"m{i}") for i in range(4)]
        per_source = aggregate(many + [ev(Source.NEWS, -0.9, 0.9)], NOW,
                               self.s.strategy)
        self.assertLess(abs(per_source[Source.MARKET]), 0.35)
        self.assertGreater(abs(per_source[Source.NEWS]), 0.7)


class SessionGateTest(unittest.TestCase):
    def setUp(self):
        self.s = Settings.load()
        self.aligned = [ev(Source.NEWS, 0.85, 0.9), ev(Source.OPTIONS_FLOW, 0.8, 0.9)]

    def _at(self, when: datetime):
        import dataclasses
        snap = dataclasses.replace(snapshot(), now=when)
        return assess("NVDA", [
            dataclasses.replace(e, observed_at=when) for e in self.aligned
        ], snap, when, self.s.strategy)

    def test_blocked_at_the_open(self):
        a = self._at(datetime(2026, 3, 10, 13, 31, tzinfo=UTC))   # 09:31 ET
        self.assertIn("open", a.blocked)

    def test_blocked_near_the_close(self):
        a = self._at(datetime(2026, 3, 10, 19, 50, tzinfo=UTC))   # 15:50 ET
        self.assertIn("close", a.blocked)

    def test_blocked_in_premarket(self):
        a = self._at(datetime(2026, 3, 10, 12, 0, tzinfo=UTC))    # 08:00 ET
        self.assertIn("premarket", a.blocked)

    def test_blocked_on_a_holiday(self):
        a = self._at(datetime(2026, 1, 19, 15, 0, tzinfo=UTC))    # MLK Day
        self.assertIn("closed", a.blocked)

    def test_allowed_mid_session(self):
        a = self._at(datetime(2026, 3, 10, 16, 0, tzinfo=UTC))    # 12:00 ET
        self.assertTrue(a.would_trade, a.blocked)


class IntentTest(unittest.TestCase):
    def setUp(self):
        self.s = Settings.load()

    def _intent(self, **snap_kw):
        a = assess(
            "NVDA",
            [ev(Source.NEWS, 0.85, 0.9), ev(Source.OPTIONS_FLOW, 0.8, 0.9)],
            snapshot(**snap_kw), NOW, self.s.strategy,
        )
        return build_intent(a, snapshot(**snap_kw), self.s.strategy, self.s.risk)

    def test_long_intent_has_a_stop_below_entry(self):
        i = self._intent()
        self.assertIsNotNone(i)
        self.assertLess(i.stop_price, i.reference_price)
        self.assertGreater(i.target_price, i.reference_price)

    def test_reward_risk_meets_the_minimum(self):
        i = self._intent()
        self.assertGreaterEqual(i.reward_risk, self.s.strategy.min_reward_risk)

    def test_stop_respects_the_percentage_floor(self):
        """For a very quiet name, an ATR-derived stop can land inside the
        spread. The floor keeps it outside."""
        cfg, risk = StrategyConfig(), RiskConfig()
        stop = stop_for(Side.BUY, 100.0, 0.01, cfg, risk)
        self.assertLessEqual(stop, 100.0 * (1 - risk.min_stop_pct) + 1e-9)

    def test_stop_is_pushed_beyond_session_structure(self):
        cfg, risk = StrategyConfig(), RiskConfig()
        stop = stop_for(Side.BUY, 100.0, 1.0, cfg, risk, session_low=98.5)
        self.assertLess(stop, 98.5)

    def test_short_stop_sits_above_entry(self):
        cfg, risk = StrategyConfig(), RiskConfig()
        stop = stop_for(Side.SELL, 100.0, 2.0, cfg, risk, session_high=101.5)
        self.assertGreater(stop, 101.5)

    def test_intent_rejects_a_stop_on_the_wrong_side(self):
        from qd.types import Intent
        with self.assertRaises(ValueError):
            Intent("NVDA", Side.BUY, 0.5, 100.0, 101.0, 105.0, NOW)
        with self.assertRaises(ValueError):
            Intent("NVDA", Side.SELL, 0.5, 100.0, 99.0, 95.0, NOW)

    def test_idempotency_key_is_stable_within_the_minute(self):
        from qd.types import Intent
        a = Intent("NVDA", Side.BUY, 0.5, 100.0, 98.0, 104.0, NOW)
        b = Intent("NVDA", Side.BUY, 0.7, 100.0, 98.0, 104.0,
                   NOW + timedelta(seconds=30))
        self.assertEqual(a.idempotency_key(), b.idempotency_key())

    def test_idempotency_key_differs_across_symbols_and_sides(self):
        from qd.types import Intent
        a = Intent("NVDA", Side.BUY, 0.5, 100.0, 98.0, 104.0, NOW)
        b = Intent("AMD", Side.BUY, 0.5, 100.0, 98.0, 104.0, NOW)
        c = Intent("NVDA", Side.SELL, 0.5, 100.0, 102.0, 96.0, NOW)
        self.assertNotEqual(a.idempotency_key(), b.idempotency_key())
        self.assertNotEqual(a.idempotency_key(), c.idempotency_key())

    def test_intent_carries_its_own_justification(self):
        i = self._intent()
        self.assertTrue(i.evidence)
        self.assertGreaterEqual(len(i.sources()), 2)


if __name__ == "__main__":
    unittest.main()
