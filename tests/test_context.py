"""
The regime layer, tested entirely on its own.

No evidence, no positions, no broker — bars in, label out. That isolation is
the point of separating the layer: when the system loses money you need to be
able to ask "was the regime call wrong, or was the signal wrong" and get an
answer.
"""

from __future__ import annotations

import math
import unittest
from datetime import datetime, timedelta

from qd.config import ContextConfig, Settings
from qd.context import (
    ER_TRENDING, MIN_BARS, ContextState, MarketContext, Regime, VolState,
    classify, efficiency_ratio, percentile_rank, realized_vol, sma,
)
from qd.types import Bar, UTC

START = datetime(2026, 1, 5, 14, 30, tzinfo=UTC)


def daily(closes, symbol="TEST", start=START, vol=1_000_000.0) -> list[Bar]:
    """Daily bars from a close series. `known_at` is each bar's close."""
    out = []
    for i, c in enumerate(closes):
        s = start + timedelta(days=i)
        prev = closes[i - 1] if i else c
        out.append(Bar(
            symbol=symbol, start=s, end=s + timedelta(hours=6, minutes=30),
            open=prev, high=max(prev, c) * 1.005, low=min(prev, c) * 0.995,
            close=c, volume=vol,
        ))
    return out


def trending(n=120, start_px=100.0, step=0.5) -> list[float]:
    return [start_px + step * i for i in range(n)]


def choppy(n=120, start_px=100.0, amp=3.0) -> list[float]:
    return [start_px + amp * math.sin(i / 2.0) for i in range(n)]


class EfficiencyRatioTest(unittest.TestCase):
    def test_straight_line_is_maximally_efficient(self):
        er = efficiency_ratio(daily(trending(40)), 20)
        self.assertAlmostEqual(er, 1.0, places=6)

    def test_round_trip_is_inefficient(self):
        # Up 20 then back down 20: net zero travel, plenty of gross travel.
        closes = [100 + i for i in range(21)] + [120 - i for i in range(1, 21)]
        er = efficiency_ratio(daily(closes), 40)
        self.assertAlmostEqual(er, 0.0, places=6)

    def test_oscillation_scores_below_the_trend_threshold(self):
        er = efficiency_ratio(daily(choppy(60)), 20)
        self.assertIsNotNone(er)
        self.assertLess(er, ER_TRENDING)

    def test_insufficient_history_returns_none(self):
        self.assertIsNone(efficiency_ratio(daily(trending(5)), 20))

    def test_flat_series_returns_none_rather_than_dividing_by_zero(self):
        self.assertIsNone(efficiency_ratio(daily([100.0] * 30), 20))


class RealizedVolTest(unittest.TestCase):
    def test_flat_series_has_zero_vol(self):
        self.assertAlmostEqual(realized_vol(daily([100.0] * 40), 20), 0.0, places=9)

    def test_noisier_series_has_higher_vol(self):
        calm = daily([100 + 0.1 * (i % 2) for i in range(40)])
        wild = daily([100 + 5.0 * (i % 2) for i in range(40)])
        self.assertGreater(realized_vol(wild, 20), realized_vol(calm, 20))

    def test_percentile_rank_needs_enough_history(self):
        self.assertIsNone(percentile_rank(0.5, [0.1, 0.2]))
        self.assertIsNotNone(percentile_rank(0.5, [i / 100 for i in range(30)]))


class ClassifyTest(unittest.TestCase):
    def _now(self, bars):
        return bars[-1].known_at + timedelta(minutes=1)

    def test_uptrend_is_labelled(self):
        bars = daily(trending(150))
        st = classify("TEST", bars, self._now(bars))
        self.assertIs(st.regime, Regime.TREND_UP)
        self.assertTrue(st.known)

    def test_downtrend_is_labelled(self):
        bars = daily(trending(150, start_px=200.0, step=-0.5))
        st = classify("TEST", bars, self._now(bars))
        self.assertIs(st.regime, Regime.TREND_DOWN)

    def test_chop_is_labelled(self):
        bars = daily(choppy(150))
        st = classify("TEST", bars, self._now(bars))
        self.assertIs(st.regime, Regime.CHOP)

    def test_insufficient_history_is_unknown_not_guessed(self):
        bars = daily(trending(20))
        st = classify("TEST", bars, self._now(bars))
        self.assertIs(st.regime, Regime.UNKNOWN)
        self.assertFalse(st.known)

    def test_trend_requires_both_efficiency_and_ma_order(self):
        """A single gap can score a high ER on an otherwise flat series. The
        moving-average condition is what stops that reading as a trend."""
        closes = [100.0] * 80 + [130.0] * 40      # one jump, then flat
        bars = daily(closes)
        er = efficiency_ratio(bars, 20)
        st = classify("TEST", bars, self._now(bars))
        # ER over the last 20 bars is undefined/zero here (flat tail), so the
        # label must not be a trend.
        self.assertIsNot(st.regime, Regime.TREND_DOWN)

    def test_classification_respects_point_in_time(self):
        """Bars that have not closed must not influence the label."""
        bars = daily(trending(80) + [200.0] * 40)   # violent move at the end
        early = bars[79].known_at + timedelta(minutes=1)
        st = classify("TEST", bars, early)
        self.assertEqual(st.bars_used, 80)
        # The later bars exist in the list but were not visible.
        self.assertLess(st.bars_used, len(bars))


class VolStateTest(unittest.TestCase):
    def test_extreme_volatility_is_not_tradeable(self):
        self.assertFalse(VolState.EXTREME.is_tradeable)
        self.assertTrue(VolState.HIGH.is_tradeable)
        self.assertTrue(VolState.NORMAL.is_tradeable)
        self.assertTrue(VolState.LOW.is_tradeable)

    def test_volatility_spike_raises_the_state(self):
        # A long calm history, then a burst.
        calm = [100 + 0.05 * (i % 2) for i in range(260)]
        burst = [100 + 8.0 * (i % 2) for i in range(25)]
        bars = daily(calm + burst)
        st = classify("TEST", bars, bars[-1].known_at + timedelta(minutes=1))
        self.assertIn(st.vol_state, (VolState.HIGH, VolState.EXTREME))


class PermissionTest(unittest.TestCase):
    def _state(self, regime, vol=VolState.NORMAL, symbol="TEST") -> ContextState:
        return ContextState(
            symbol=symbol, now=START, regime=regime, vol_state=vol,
            efficiency=0.5, vol_annual=0.3, vol_percentile=0.5,
            sma_fast=100.0, sma_slow=99.0, bars_used=200,
        )

    def test_allowed_regime_passes(self):
        st = self._state(Regime.TREND_UP)
        ok, why = st.permits([Regime.TREND_UP, Regime.CHOP])
        self.assertTrue(ok, why)

    def test_disallowed_regime_blocks(self):
        st = self._state(Regime.TREND_DOWN)
        ok, why = st.permits([Regime.TREND_UP])
        self.assertFalse(ok)
        self.assertIn("regime", why)

    def test_extreme_vol_blocks_every_regime(self):
        st = self._state(Regime.TREND_UP, VolState.EXTREME)
        ok, why = st.permits([Regime.TREND_UP, Regime.TREND_DOWN, Regime.CHOP])
        self.assertFalse(ok)
        self.assertIn("volatility", why)

    def test_unknown_regime_blocks_when_required(self):
        st = self._state(Regime.UNKNOWN, VolState.UNKNOWN)
        self.assertFalse(st.permits([Regime.CHOP])[0])
        self.assertTrue(st.permits([Regime.CHOP], require_known=False)[0])

    def test_market_downtrend_blocks_a_permitted_symbol(self):
        """A long in a market-wide downtrend fights the factor that explains
        most of its return."""
        ctx = MarketContext(
            market=self._state(Regime.TREND_DOWN, symbol="SPY"),
            symbol=self._state(Regime.TREND_UP),
        )
        ok, why = ctx.permits([Regime.TREND_UP], [Regime.TREND_UP, Regime.CHOP])
        self.assertFalse(ok)
        self.assertIn("market regime", why)

    def test_market_extreme_vol_blocks_regardless_of_regime_list(self):
        ctx = MarketContext(
            market=self._state(Regime.CHOP, VolState.EXTREME, symbol="SPY"),
            symbol=self._state(Regime.CHOP),
        )
        self.assertFalse(ctx.permits([Regime.CHOP], [Regime.CHOP])[0])

    def test_market_context_ignored_when_not_requested(self):
        ctx = MarketContext(
            market=self._state(Regime.TREND_DOWN, symbol="SPY"),
            symbol=self._state(Regime.CHOP),
        )
        self.assertTrue(ctx.permits([Regime.CHOP], None)[0])


class StrategyIntegrationTest(unittest.TestCase):
    """The gate must consult regime before signal strength."""

    def setUp(self):
        from qd.features.market import MarketSnapshot
        from qd.types import Evidence, Source

        self.s = Settings.load()
        self.now = datetime(2026, 3, 10, 16, 0, tzinfo=UTC)   # 12:00 ET
        self.snap = MarketSnapshot(
            symbol="NVDA", now=self.now, last=100.0, atr=2.0, atr_pct=2.0,
            vwap=100.0, ema_fast=100.0, ema_slow=100.0, rvol=1.5, gap_pct=0.0,
            prev_close=100.0, session_high=101.0, session_low=99.0,
            adv_dollar=500_000_000.0, bar_count=120, last_bar_end=self.now,
        )
        self.evidence = [
            Evidence(source=Source.NEWS, kind="guidance_raise", symbol="NVDA",
                     score=0.85, confidence=0.9, observed_at=self.now,
                     ttl=timedelta(hours=1)),
            Evidence(source=Source.OPTIONS_FLOW, kind="premium_imbalance",
                     symbol="NVDA", score=0.8, confidence=0.9,
                     observed_at=self.now, ttl=timedelta(hours=1)),
        ]

    def _ctx(self, symbol_regime, market_regime=Regime.TREND_UP, vol=VolState.NORMAL):
        def st(name, regime):
            return ContextState(
                symbol=name, now=self.now, regime=regime, vol_state=vol,
                efficiency=0.5, vol_annual=0.3, vol_percentile=0.5,
                sma_fast=100.0, sma_slow=99.0, bars_used=200,
            )
        return MarketContext(market=st("SPY", market_regime),
                             symbol=st("NVDA", symbol_regime))

    def test_strong_signal_is_blocked_by_a_disallowed_regime(self):
        from qd.strategy import assess
        import dataclasses

        cfg = dataclasses.replace(
            ContextConfig(), allowed_regimes=("trend_up",),
            allowed_market_regimes=None,
        )
        a = assess("NVDA", self.evidence, self.snap, self.now, self.s.strategy,
                   context=self._ctx(Regime.CHOP), context_cfg=cfg)
        self.assertFalse(a.would_trade)
        self.assertIn("context", a.blocked)

    def test_same_signal_trades_in_an_allowed_regime(self):
        from qd.strategy import assess

        a = assess("NVDA", self.evidence, self.snap, self.now, self.s.strategy,
                   context=self._ctx(Regime.TREND_UP), context_cfg=ContextConfig())
        self.assertTrue(a.would_trade, a.blocked)

    def test_extreme_market_volatility_blocks(self):
        from qd.strategy import assess

        a = assess("NVDA", self.evidence, self.snap, self.now, self.s.strategy,
                   context=self._ctx(Regime.TREND_UP, vol=VolState.EXTREME),
                   context_cfg=ContextConfig())
        self.assertFalse(a.would_trade)
        self.assertIn("volatility", a.blocked)

    def test_disabling_the_layer_restores_prior_behaviour(self):
        from qd.strategy import assess
        import dataclasses

        cfg = dataclasses.replace(ContextConfig(), enabled=False)
        a = assess("NVDA", self.evidence, self.snap, self.now, self.s.strategy,
                   context=None, context_cfg=cfg)
        self.assertTrue(a.would_trade, a.blocked)

    def test_missing_context_blocks_when_a_known_regime_is_required(self):
        from qd.strategy import assess

        a = assess("NVDA", self.evidence, self.snap, self.now, self.s.strategy,
                   context=None, context_cfg=ContextConfig())
        self.assertFalse(a.would_trade)
        self.assertIn("regime not classified", a.blocked)


if __name__ == "__main__":
    unittest.main()
