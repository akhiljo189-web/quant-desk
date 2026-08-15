"""Risk engine: sizing, caps that only reduce, PDT, and the breakers."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.config import RiskConfig, Settings, UniverseConfig
from qd.portfolio import Portfolio
from qd.risk import overnight_check, size, validate_config
from qd.types import Intent, Position, Side, UTC

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)


def intent(sym="NVDA", side=Side.BUY, price=100.0, stop=98.0, target=104.0) -> Intent:
    return Intent(sym, side, 0.6, price, stop, target, NOW)


def portfolio(equity=100_000.0, **risk_kw):
    """Returns (portfolio, settings) — most tests need both."""
    import dataclasses
    s = Settings.load()
    if risk_kw:
        s = dataclasses.replace(s, risk=dataclasses.replace(s.risk, **risk_kw))
    p = Portfolio(equity, s.risk, s.universe)
    p.roll_day(NOW)
    return p, s


class SizingTest(unittest.TestCase):
    def test_size_follows_cash_risk_not_notional(self):
        p, s = portfolio()
        # 0.5% of 100k = $500 budget; $2.00 stop => 250 shares.
        d = size(intent(price=100.0, stop=98.0), p, s.risk, s.universe, NOW)
        self.assertTrue(d.approved)
        self.assertEqual(d.quantity, 200.0)   # capped at 20% notional = $20k
        self.assertLessEqual(d.cash_risk, 500.0)

    def test_wider_stop_gives_smaller_position(self):
        p, s = portfolio()
        tight = size(intent(price=100, stop=99), p, s.risk, s.universe, NOW)
        wide = size(intent(price=100, stop=90), p, s.risk, s.universe, NOW)
        self.assertGreater(tight.quantity, wide.quantity)
        # Both risk the same cash — that is the point of risk-first sizing.
        self.assertLessEqual(wide.cash_risk, 500.0)

    def test_conviction_does_not_change_size(self):
        p, s = portfolio()
        timid = Intent("NVDA", Side.BUY, 0.36, 100.0, 98.0, 104.0, NOW)
        certain = Intent("NVDA", Side.BUY, 0.99, 100.0, 98.0, 104.0, NOW)
        a = size(timid, p, s.risk, s.universe, NOW)
        b = size(certain, p, s.risk, s.universe, NOW)
        self.assertEqual(a.quantity, b.quantity)

    def test_stop_too_wide_for_one_share_is_rejected(self):
        p, s = portfolio(equity=1_000.0)
        # $5 budget, $50 stop distance — a single share breaches it.
        d = size(intent(price=500.0, stop=450.0), p, s.risk, s.universe, NOW)
        self.assertFalse(d.approved)
        self.assertIn("stop", d.reason)


class CapTest(unittest.TestCase):
    def test_caps_only_reduce_never_increase(self):
        p, s = portfolio()
        base = size(intent(price=10.0, stop=9.9), p, s.risk, s.universe, NOW)
        # $500 / $0.10 = 5000 shares = $50k notional, above the 20% cap.
        self.assertTrue(base.approved)
        self.assertEqual(base.notional, 20_000.0)
        self.assertEqual(base.capped_by, "position_notional")

    def test_sector_notional_cap_binds(self):
        p, s = portfolio()
        p.open(Position("AMD", Side.BUY, 200, 100.0, 98.0, 104.0, NOW))     # $20k semis
        p.open(Position("MU", Side.BUY, 150, 100.0, 98.0, 104.0, NOW))      # $15k semis
        d = size(intent("NVDA", price=100.0, stop=98.0), p, s.risk, s.universe, NOW)
        # Sector cap 35% of 100k = $35k, already used. No room.
        self.assertFalse(d.approved)

    def test_sector_position_count_cap(self):
        p, s = portfolio()
        for sym in ("AMD", "MU", "INTC"):
            p.open(Position(sym, Side.BUY, 10, 100.0, 98.0, 104.0, NOW))
        d = size(intent("NVDA"), p, s.risk, s.universe, NOW)
        self.assertFalse(d.approved)
        self.assertIn("sector", d.reason)

    def test_total_open_risk_ceiling(self):
        p, s = portfolio()
        # Four positions each risking ~0.5% => 2.0%, the ceiling.
        for sym, sec in (("AAPL", 1), ("JPM", 2), ("XOM", 3), ("UNH", 4)):
            p.open(Position(sym, Side.BUY, 250, 100.0, 98.0, 104.0, NOW))
        d = size(intent("LLY"), p, s.risk, s.universe, NOW)
        self.assertFalse(d.approved)
        self.assertIn("open risk", d.reason)

    def test_net_exposure_does_not_block_an_offsetting_trade(self):
        """A short added to a long book reduces net exposure — it must not be
        capped by it."""
        p, s = portfolio()
        p.open(Position("AAPL", Side.BUY, 900, 100.0, 98.0, 104.0, NOW))   # $90k long
        d = size(intent("XOM", side=Side.SELL, price=100.0, stop=102.0, target=96.0),
                 p, s.risk, s.universe, NOW)
        self.assertTrue(d.approved, d.reason)
        self.assertNotEqual(d.capped_by, "net_exposure")


class BreakerTest(unittest.TestCase):
    def test_daily_loss_percentage_halts(self):
        p, s = portfolio()
        p.open(Position("AAPL", Side.BUY, 100, 100.0, 98.0, 104.0, NOW))
        p.close("AAPL", 79.0, NOW, "stop")     # -$2100 = -2.1% of 100k
        self.assertTrue(p.breaker().active)
        d = size(intent(), p, s.risk, s.universe, NOW)
        self.assertFalse(d.approved)
        self.assertIn("circuit breaker", d.reason)

    def test_loss_streak_halts_even_when_small(self):
        p, s = portfolio()
        for i in range(4):
            sym = f"S{i}"
            p.open(Position(sym, Side.BUY, 1, 100.0, 99.0, 102.0, NOW))
            p.close(sym, 99.0, NOW, "stop")    # -$1 each
        self.assertTrue(p.breaker().active)
        self.assertIn("consecutive", p.breaker().reason)

    def test_a_win_resets_the_streak(self):
        p, _ = portfolio()
        for i in range(3):
            p.open(Position(f"S{i}", Side.BUY, 1, 100.0, 99.0, 102.0, NOW))
            p.close(f"S{i}", 99.0, NOW, "stop")
        self.assertEqual(p.daily_loss_streak, 3)
        p.open(Position("W", Side.BUY, 1, 100.0, 99.0, 102.0, NOW))
        p.close("W", 102.0, NOW, "target")
        self.assertEqual(p.daily_loss_streak, 0)

    def test_stale_data_blocks_entry(self):
        p, s = portfolio()
        d = size(intent(), p, s.risk, s.universe, NOW, data_stale="feed frozen 400s")
        self.assertFalse(d.approved)
        self.assertIn("stale", d.reason)

    def test_earnings_blackout_blocks_entry(self):
        p, s = portfolio()
        d = size(intent(), p, s.risk, s.universe, NOW, earnings_blackout="earnings in 3.2h")
        self.assertFalse(d.approved)
        self.assertIn("earnings", d.reason)


class PDTTest(unittest.TestCase):
    def test_small_account_blocked_at_the_limit(self):
        p, _ = portfolio(equity=10_000.0)
        for i in range(3):
            sym = f"S{i}"
            p.open(Position(sym, Side.BUY, 1, 100.0, 99.0, 102.0, NOW))
            p.close(sym, 101.0, NOW, "target")      # same-session round trips
        blocked, reason = p.pdt_blocked(NOW)
        self.assertTrue(blocked)
        self.assertIn("PDT", reason)

    def test_large_account_is_exempt(self):
        p, _ = portfolio(equity=30_000.0)
        for i in range(5):
            sym = f"S{i}"
            p.open(Position(sym, Side.BUY, 1, 100.0, 99.0, 102.0, NOW))
            p.close(sym, 101.0, NOW, "target")
        self.assertFalse(p.pdt_blocked(NOW)[0])

    def test_overnight_hold_is_not_a_day_trade(self):
        p, _ = portfolio(equity=10_000.0)
        for i in range(3):
            sym = f"S{i}"
            p.open(Position(sym, Side.BUY, 1, 100.0, 99.0, 102.0, NOW))
            # Closed the NEXT session — a swing trade, not a day trade.
            p.close(sym, 101.0, NOW + timedelta(days=1), "target")
        self.assertFalse(p.pdt_blocked(NOW + timedelta(days=1))[0])

    def test_window_counts_business_days_not_calendar_days(self):
        """Over a weekend, a calendar window would drop two sessions early."""
        p, _ = portfolio(equity=10_000.0)
        friday = datetime(2026, 3, 6, 15, 0, tzinfo=UTC)
        for i in range(3):
            sym = f"S{i}"
            p.open(Position(sym, Side.BUY, 1, 100.0, 99.0, 102.0, friday))
            p.close(sym, 101.0, friday, "target")
        # The following Wednesday is still inside 5 business days.
        wednesday = datetime(2026, 3, 11, 15, 0, tzinfo=UTC)
        self.assertEqual(p.day_trades_in_window(wednesday), 3)


class OvernightTest(unittest.TestCase):
    def test_flattens_everything_when_overnight_disallowed(self):
        import dataclasses
        s = Settings.load()
        cfg = dataclasses.replace(s.risk, allow_overnight=False)
        p = Portfolio(100_000, cfg, s.universe)
        p.roll_day(NOW)
        p.open(Position("AAPL", Side.BUY, 10, 100.0, 98.0, 104.0, NOW))
        self.assertEqual(overnight_check(p, cfg, NOW), ["AAPL"])

    def test_keeps_only_the_lowest_risk_positions(self):
        import dataclasses
        s = Settings.load()
        cfg = dataclasses.replace(s.risk, allow_overnight=True, max_overnight_positions=1)
        p = Portfolio(100_000, cfg, s.universe)
        p.roll_day(NOW)
        p.open(Position("AAPL", Side.BUY, 100, 100.0, 99.0, 104.0, NOW))   # $100 risk
        p.open(Position("XOM", Side.BUY, 100, 100.0, 90.0, 104.0, NOW))    # $1000 risk
        # The riskiest goes first.
        self.assertEqual(overnight_check(p, cfg, NOW), ["XOM"])


class ConfigValidationTest(unittest.TestCase):
    def test_default_config_is_coherent(self):
        self.assertEqual(validate_config(RiskConfig()), [])

    def test_catches_unreachable_risk_ceiling(self):
        import dataclasses
        bad = dataclasses.replace(RiskConfig(), risk_pct=3.0, max_total_open_risk_pct=1.0)
        self.assertTrue(any("no trade could ever" in p for p in validate_config(bad)))

    def test_catches_inverted_breakers(self):
        import dataclasses
        bad = dataclasses.replace(RiskConfig(), daily_loss_stop_pct=5.0, weekly_loss_stop_pct=2.0)
        self.assertTrue(any("weekly breaker can never" in p for p in validate_config(bad)))


if __name__ == "__main__":
    unittest.main()


class RMultipleSurvivesStopMovesTest(unittest.TestCase):
    """R is measured against the risk taken AT ENTRY, permanently.

    Found by audit, in the exit mix of the first corrected evaluation: `target`
    exits averaged +0.305R when the target sits at 2R. The cause was that
    `risk_per_share` read the CURRENT stop, and `breakeven_after_partial` moves
    the stop to entry — making the denominator zero and r_multiple 0.0 for
    every trade that had gone well enough to bank a partial.

    The damage is doubly compounding: winners were recorded as 0.0R, and
    win_rate counts `r > 0`, so those same winners were then counted as
    LOSSES. Expectancy and win rate were both dragged down by exactly the
    trades that worked.
    """

    def position(self, stop=98.0):
        return Position(
            symbol="AAA", side=Side.BUY, quantity=100, entry_price=100.0,
            stop_price=stop, target_price=104.0,
            opened_at=datetime(2026, 3, 10, 14, 30, tzinfo=UTC),
        )

    def test_r_is_unchanged_when_the_stop_moves_to_breakeven(self):
        pos = self.position()
        self.assertAlmostEqual(pos.r_multiple(104.0), 2.0)
        pos.stop_price = pos.entry_price          # breakeven move
        self.assertAlmostEqual(pos.r_multiple(104.0), 2.0,
                               msg="R collapsed when the stop moved")

    def test_a_winner_never_reports_zero_r(self):
        pos = self.position()
        pos.stop_price = pos.entry_price
        self.assertGreater(pos.r_multiple(103.0), 0.0)

    def test_a_trailed_stop_does_not_inflate_r(self):
        """Trailing the stop UP would shrink the denominator and inflate R —
        the same bug in the flattering direction."""
        pos = self.position()
        pos.stop_price = 101.0                    # trailed into profit
        self.assertAlmostEqual(pos.r_multiple(104.0), 2.0)

    def test_open_risk_does_follow_the_live_stop(self):
        """Open risk is a FORWARD-looking exposure number and must track the
        real stop; only the R yardstick is frozen at entry."""
        pos = self.position()
        self.assertAlmostEqual(pos.open_risk, 200.0)
        pos.stop_price = pos.entry_price
        self.assertAlmostEqual(pos.open_risk, 0.0)

    def test_a_zero_width_entry_stop_is_still_zero_r(self):
        pos = self.position(stop=100.0)
        self.assertEqual(pos.r_multiple(105.0), 0.0)


class PartialProfitIsCountedTest(unittest.TestCase):
    """Profit banked by a partial take belongs in the trade's result.

    The engine closes half the position at +1R and lets the rest run. That
    banked half reached broker equity but never reached the trade ledger: the
    ClosedTrade recorded only what the REMAINDER did. 66 of 225 trades in the
    first corrected evaluation took a partial, so roughly a third of the
    sample was reported with its best-performing half deleted.
    """

    def portfolio(self):
        from qd.config import RiskConfig, UniverseConfig
        return Portfolio(100_000.0, RiskConfig(), UniverseConfig())

    def opened(self, p):
        from qd.types import Intent
        p.open(Position(
            symbol="AAA", side=Side.BUY, quantity=100, entry_price=100.0,
            stop_price=98.0, target_price=104.0,
            opened_at=datetime(2026, 3, 10, 14, 30, tzinfo=UTC),
        ))
        return p.get("AAA")

    def test_a_partial_contributes_to_the_final_r(self):
        p = self.portfolio()
        pos = self.opened(p)
        # Bank half at +1R (102.0), then the rest exits at breakeven.
        p.take_partial("AAA", 50, 102.0, datetime(2026, 3, 11, 14, 30, tzinfo=UTC))
        trade = p.close("AAA", 100.0, datetime(2026, 3, 12, 14, 30, tzinfo=UTC), "stop")
        # Half the position made +1R, half made 0R: +0.5R overall, not 0R.
        self.assertAlmostEqual(trade.r_multiple, 0.5, places=6)
        self.assertGreater(trade.pnl, 0.0)

    def test_without_a_partial_nothing_changes(self):
        p = self.portfolio()
        self.opened(p)
        trade = p.close("AAA", 104.0, datetime(2026, 3, 12, 14, 30, tzinfo=UTC), "target")
        self.assertAlmostEqual(trade.r_multiple, 2.0, places=6)

    def test_the_partial_reduces_the_open_quantity(self):
        p = self.portfolio()
        self.opened(p)
        p.take_partial("AAA", 40, 102.0, datetime(2026, 3, 11, 14, 30, tzinfo=UTC))
        self.assertEqual(p.get("AAA").quantity, 60)

    def test_a_partial_on_a_missing_position_is_a_noop(self):
        p = self.portfolio()
        self.assertIsNone(p.take_partial("ZZZ", 10, 100.0,
                                         datetime(2026, 3, 11, tzinfo=UTC)))
