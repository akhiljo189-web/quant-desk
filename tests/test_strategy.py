"""
Strategy: the role model — trigger, confirm, veto — and proof it CAN trade.

A system that never trades passes every safety test trivially, so the tests
come in pairs: one showing a gate blocks what it should, one showing the same
gate lets a genuinely aligned setup through. Without the second half, "no
trades" and "correctly cautious" are indistinguishable.

The central contract asserted here, which the docs registered and the code now
enforces:

    no earnings evidence  -> no trade, whatever the other channels say
    news                  -> can only veto, never add conviction
    confirmations         -> scale conviction up to the trigger, never past it
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
       age=timedelta(0), ttl=timedelta(hours=6)) -> Evidence:
    return Evidence(
        source=source, kind=kind, symbol="NVDA", score=score, confidence=conf,
        observed_at=NOW - age, ttl=ttl,
    )


def pead(score: float, conf: float = 0.9) -> Evidence:
    return ev(Source.EARNINGS, score, conf, kind="pead")


class TriggerTest(unittest.TestCase):
    """No earnings evidence, no trade — the headline rule."""

    def setUp(self):
        self.s = Settings.load()

    def test_news_and_flow_alone_cannot_trade(self):
        """THE regression test for the hypothesis restructure. Under the old
        symmetric blend this exact evidence traded; it must never again."""
        a = assess(
            "NVDA",
            [ev(Source.NEWS, 0.95, 0.95), ev(Source.OPTIONS_FLOW, 0.9, 0.9),
             ev(Source.MARKET, 0.8, 0.8)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertFalse(a.would_trade)
        self.assertIn("no PEAD trigger", a.blocked)

    def test_trigger_plus_confirmation_trades(self):
        a = assess(
            "NVDA", [pead(0.7), ev(Source.OPTIONS_FLOW, 0.6, 0.85)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertTrue(a.would_trade, a.blocked)
        self.assertIs(a.direction, Side.BUY)

    def test_negative_drift_goes_short(self):
        a = assess(
            "NVDA", [pead(-0.7), ev(Source.MARKET, -0.5, 0.8)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertTrue(a.would_trade, a.blocked)
        self.assertIs(a.direction, Side.SELL)

    def test_trigger_alone_fails_confluence(self):
        a = assess("NVDA", [pead(0.9)], snapshot(), NOW, self.s.strategy)
        self.assertFalse(a.would_trade)
        self.assertIn("confluence", a.blocked)

    def test_weak_trigger_is_a_shrug_not_a_trade(self):
        a = assess(
            "NVDA", [pead(0.08), ev(Source.OPTIONS_FLOW, 0.9, 0.9)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertFalse(a.would_trade)
        self.assertIn("below floor", a.blocked)

    def test_expired_trigger_does_not_count(self):
        a = assess(
            "NVDA",
            [ev(Source.EARNINGS, 0.9, 0.9, kind="pead",
                age=timedelta(hours=8), ttl=timedelta(hours=1)),
             ev(Source.OPTIONS_FLOW, 0.8, 0.9)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertFalse(a.would_trade)
        self.assertIn("no PEAD trigger", a.blocked)

    def test_direction_follows_the_trigger_not_the_confirmations(self):
        """Even a loud opposing confirmation cannot flip the direction — it can
        only block. Direction is the drift's sign by construction."""
        a = assess(
            "NVDA", [pead(0.6), ev(Source.MARKET, -0.9, 0.9)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertIs(a.direction, Side.BUY)
        self.assertFalse(a.would_trade)   # blocked by conflict, not redirected


class VetoTest(unittest.TestCase):
    def setUp(self):
        self.s = Settings.load()
        self.base = [pead(0.7), ev(Source.OPTIONS_FLOW, 0.6, 0.85)]

    def test_opposing_news_vetoes(self):
        a = assess(
            "NVDA", self.base + [ev(Source.NEWS, -0.8, 0.9, kind="guidance_cut")],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertFalse(a.would_trade)
        self.assertIn("news veto", a.blocked)

    def test_agreeing_news_adds_nothing(self):
        """The demotion, asserted: a headline agreeing with the drift must not
        raise conviction, because at our latency agreement is not information."""
        without = assess("NVDA", self.base, snapshot(), NOW, self.s.strategy)
        with_news = assess(
            "NVDA", self.base + [ev(Source.NEWS, 0.9, 0.95, kind="guidance_raise")],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertTrue(without.would_trade)
        self.assertTrue(with_news.would_trade)
        self.assertAlmostEqual(without.conviction, with_news.conviction, places=9)
        self.assertEqual(without.agreeing_sources, with_news.agreeing_sources)

    def test_mild_opposing_news_reduces_nothing_but_is_recorded(self):
        """Below the veto threshold the trade proceeds, but the journal keeps
        the opposing reading — that is what you audit after a bad week."""
        a = assess(
            "NVDA", self.base + [ev(Source.NEWS, -0.2, 0.9)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertTrue(a.would_trade, a.blocked)
        self.assertLess(a.veto_score, 0.0)


class ConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.s = Settings.load()

    def test_confirmations_lift_conviction(self):
        one = assess(
            "NVDA", [pead(0.7), ev(Source.OPTIONS_FLOW, 0.5, 0.8)],
            snapshot(), NOW, self.s.strategy,
        )
        two = assess(
            "NVDA",
            [pead(0.7), ev(Source.OPTIONS_FLOW, 0.5, 0.8), ev(Source.MARKET, 0.6, 0.8)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertGreater(two.conviction, one.conviction)

    def test_conviction_never_exceeds_the_trigger(self):
        """Confirmations scale conviction up to the trigger's own strength and
        no further — enthusiasm cannot manufacture drift."""
        a = assess(
            "NVDA",
            [pead(0.5),
             ev(Source.OPTIONS_FLOW, 1.0, 1.0), ev(Source.MARKET, 1.0, 1.0)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertLessEqual(a.conviction, abs(a.trigger_score) + 1e-9)

    def test_opposing_confirmations_block_on_conflict(self):
        a = assess(
            "NVDA",
            [pead(0.7), ev(Source.OPTIONS_FLOW, -0.8, 0.9), ev(Source.MARKET, 0.6, 0.8)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertFalse(a.would_trade)

    def test_a_shrug_does_not_count_as_confirmation(self):
        a = assess(
            "NVDA", [pead(0.9), ev(Source.OPTIONS_FLOW, 0.02, 0.9)],
            snapshot(), NOW, self.s.strategy,
        )
        self.assertFalse(a.would_trade)
        self.assertIn("confluence", a.blocked)

    def test_many_weak_market_readings_do_not_outvote_one_strong_channel(self):
        """Averaging within a source stops a channel that happens to emit four
        readings from dominating one that emits a single stronger one."""
        many = [ev(Source.MARKET, 0.2, 0.5, kind=f"m{i}") for i in range(4)]
        per_source = aggregate(many + [ev(Source.EARNINGS, -0.9, 0.9)], NOW,
                               self.s.strategy)
        self.assertLess(abs(per_source[Source.MARKET]), 0.35)
        self.assertGreater(abs(per_source[Source.EARNINGS]), 0.7)


class SessionGateTest(unittest.TestCase):
    def setUp(self):
        self.s = Settings.load()
        self.aligned = [pead(0.8), ev(Source.OPTIONS_FLOW, 0.7, 0.9)]

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
            "NVDA", [pead(0.8), ev(Source.OPTIONS_FLOW, 0.7, 0.9)],
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
        self.assertIn(Source.EARNINGS, i.sources())


class ExitConfigTest(unittest.TestCase):
    """The exit clock must match the registered drift horizon."""

    def test_drift_window_is_multi_day(self):
        cfg = StrategyConfig()
        self.assertGreaterEqual(cfg.max_hold, timedelta(days=5))
        self.assertLessEqual(cfg.max_hold, timedelta(days=10))

    def test_early_cut_precedes_the_window_end(self):
        cfg = StrategyConfig()
        self.assertLess(cfg.time_stop, cfg.max_hold)


if __name__ == "__main__":
    unittest.main()
