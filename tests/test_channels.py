"""The four evidence channels: market, news, earnings, options flow."""

from __future__ import annotations

import random
import unittest
from datetime import datetime, timedelta

from qd.config import (
    EarningsConfig, MarketConfig, NewsConfig, OptionsFlowConfig, Settings,
)
from qd.features import earnings as ech
from qd.features import market as mch
from qd.features import news as nch
from qd.features import optionsflow as fch
from qd.types import (
    Aggressor, Bar, EarningsEvent, NewsItem, OptionContract, OptionTrade,
    Right, Source, UTC,
)

NOW = datetime(2026, 3, 10, 15, 0, tzinfo=UTC)
EXPIRY = NOW + timedelta(days=14)


def trade(strike, right, price, size, offset_ms=0, exch="CBOE",
          bid=None, ask=None, oi=100, spot=100.0, expiry=None) -> OptionTrade:
    mid = price
    bid = bid if bid is not None else mid - 0.05
    ask = ask if ask is not None else mid + 0.05
    ts = NOW - timedelta(minutes=5) + timedelta(milliseconds=offset_ms)
    return OptionTrade(
        contract=OptionContract("ACME", expiry or EXPIRY, strike, right,
                                f"O:ACME{strike}{right.value}"),
        ts=ts, price=price, size=size, exchange=exch,
        nbbo_bid=bid, nbbo_ask=ask, underlying_price=spot,
        open_interest=oi, received_at=ts,
    )


# ─────────────────────────────────────────────────────────────────────────────
# News
# ─────────────────────────────────────────────────────────────────────────────

class NewsClassificationTest(unittest.TestCase):
    def test_directional_priors(self):
        cases = [
            ("Acme agrees to be acquired for $50/share", "acquisition_target", 1),
            ("Acme prices $500M common stock offering", "offering", -1),
            ("Acme raises FY26 guidance above consensus", "guidance_raise", 1),
            ("Acme cuts Q3 outlook on weak demand", "guidance_cut", -1),
            ("Acme wins FDA approval for lead drug", "fda_approval", 1),
            ("Acme receives complete response letter", "fda_rejection", -1),
            ("Acme files for Chapter 11 bankruptcy", "bankruptcy", -1),
            ("Acme announces $2B share repurchase program", "buyback", 1),
        ]
        for headline, category, sign in cases:
            with self.subTest(headline=headline):
                c = nch.classify(headline)
                self.assertIsNotNone(c, headline)
                self.assertEqual(c.category, category)
                self.assertEqual(c.score > 0, sign > 0)

    def test_hedged_language_reduces_conviction(self):
        firm = nch.classify("Acme agrees to be acquired for $50/share")
        rumour = nch.classify("Acme reportedly in talks to be acquired")
        self.assertLess(abs(rumour.score), abs(firm.score))
        self.assertLess(rumour.confidence, firm.confidence)
        self.assertTrue(rumour.hedged)

    def test_negation_outside_the_match_flips_the_sign(self):
        c = nch.classify("Acme calls off merger agreement with BigCo")
        self.assertTrue(c.negated)
        self.assertLess(c.score, 0)

    def test_negation_inside_the_match_does_not_double_negate(self):
        """'declines to approve' IS the rejection pattern — flipping it would
        report a rejection as an approval."""
        c = nch.classify("FDA declines to approve Acme drug")
        self.assertEqual(c.category, "fda_rejection")
        self.assertLess(c.score, 0)

    def test_unrecognised_headline_returns_none(self):
        self.assertIsNone(nch.classify("Acme CEO to speak at industry conference"))

    def test_ma_role_decides_the_sign(self):
        item = NewsItem(
            id="1", symbols=("ACME", "BIGCO"),
            headline="BigCo to acquire Acme Corp in $12B deal",
            published_at=NOW - timedelta(minutes=1),
            received_at=NOW - timedelta(seconds=50), source="reuters",
        )
        cfg = NewsConfig()
        target = nch.evaluate("ACME", [item], NOW, cfg)[0]
        acquirer = nch.evaluate("BIGCO", [item], NOW, cfg)[0]
        self.assertGreater(target.score, 0.5)
        self.assertLess(acquirer.score, 0)

    def test_repeats_lose_confidence(self):
        cfg = NewsConfig()
        tracker = nch.NoveltyTracker(cfg.novelty_window)
        confidences = []
        for i in range(3):
            item = NewsItem(
                id=str(i), symbols=("ACME",),
                headline="Acme raises FY26 guidance above consensus",
                published_at=NOW - timedelta(seconds=60),
                received_at=NOW - timedelta(seconds=50), source="reuters",
            )
            got = nch.evaluate("ACME", [item], NOW, cfg, tracker)
            confidences.append(got[0].confidence if got else 0.0)
        self.assertGreater(confidences[0], confidences[1])
        self.assertGreaterEqual(confidences[1], confidences[2])

    def test_headline_that_reached_us_late_is_dropped(self):
        """A headline that took half an hour to arrive has been tradeable by
        faster participants for half an hour. The latency penalty puts it under
        the confidence floor, so it is discarded rather than chased."""
        cfg = NewsConfig()
        fresh = NewsItem(
            id="a", symbols=("ACME",), headline="Acme raises FY26 guidance",
            published_at=NOW - timedelta(seconds=30),
            received_at=NOW - timedelta(seconds=20), source="reuters",
        )
        slow = NewsItem(
            id="b", symbols=("ACME",), headline="Acme raises FY26 guidance",
            published_at=NOW - timedelta(minutes=30),
            received_at=NOW - timedelta(seconds=20), source="reuters",
        )
        self.assertTrue(nch.evaluate("ACME", [fresh], NOW, cfg))
        self.assertEqual(nch.evaluate("ACME", [slow], NOW, cfg), [])

    def test_low_tier_source_scores_below_a_primary_wire(self):
        cfg = NewsConfig()

        def item(source: str) -> NewsItem:
            return NewsItem(
                id=source, symbols=("ACME",),
                headline="Acme raises FY26 guidance above consensus",
                published_at=NOW - timedelta(seconds=30),
                received_at=NOW - timedelta(seconds=20), source=source,
            )

        wire = nch.evaluate("ACME", [item("reuters")], NOW, cfg)[0]
        blog = nch.evaluate("ACME", [item("seeking alpha")], NOW, cfg)[0]
        self.assertGreater(wire.confidence, blog.confidence)

    def test_future_news_is_never_visible(self):
        cfg = NewsConfig()
        future = NewsItem(
            id="f", symbols=("ACME",), headline="Acme raises FY26 guidance",
            published_at=NOW + timedelta(minutes=5),
            received_at=NOW + timedelta(minutes=5), source="reuters",
        )
        self.assertEqual(nch.evaluate("ACME", [future], NOW, cfg), [])


# ─────────────────────────────────────────────────────────────────────────────
# Options flow
# ─────────────────────────────────────────────────────────────────────────────

class AggressorTest(unittest.TestCase):
    def test_lift_of_the_offer_is_a_buy(self):
        t = trade(105, Right.CALL, 2.10, 100, bid=2.00, ask=2.10)
        self.assertIs(t.aggressor(), Aggressor.BUY)

    def test_hit_of_the_bid_is_a_sell(self):
        t = trade(105, Right.CALL, 2.00, 100, bid=2.00, ask=2.10)
        self.assertIs(t.aggressor(), Aggressor.SELL)

    def test_inside_the_spread_stays_unclassified(self):
        t = trade(105, Right.CALL, 2.05, 100, bid=2.00, ask=2.10)
        self.assertIs(t.aggressor(), Aggressor.MID)

    def test_no_quote_means_unknown(self):
        t = trade(105, Right.CALL, 2.05, 100)
        t = OptionTrade(t.contract, t.ts, t.price, t.size, t.exchange, (),
                        None, None, 100.0)
        self.assertIs(t.aggressor(), Aggressor.UNKNOWN)

    def test_opening_detection_uses_open_interest(self):
        self.assertTrue(trade(105, Right.CALL, 2.10, 500, oi=100).opens_position_likely())
        self.assertFalse(trade(105, Right.CALL, 2.10, 50, oi=100).opens_position_likely())


class StructureTest(unittest.TestCase):
    def setUp(self):
        self.cfg = OptionsFlowConfig()

    def test_straddle_is_detected_and_scores_nothing(self):
        """Buying a call and a put together is a volatility bet with no
        direction. Scoring the call leg alone is the classic flow error."""
        trades = [
            trade(100, Right.CALL, 3.00, 500, 0, bid=2.90, ask=3.00),
            trade(100, Right.PUT, 2.80, 500, 50, bid=2.70, ask=2.80),
        ]
        labels = fch.detect_structures(trades, self.cfg)
        self.assertEqual(set(labels.values()), {"straddle"})
        self.assertEqual(fch.evaluate("ACME", trades, NOW, self.cfg), [])

    def test_vertical_spread_is_discounted_not_ignored(self):
        trades = [
            trade(100, Right.CALL, 3.00, 500, 0, bid=2.90, ask=3.00),    # bought
            trade(105, Right.CALL, 1.00, 500, 50, bid=1.00, ask=1.10),   # sold
        ]
        labels = fch.detect_structures(trades, self.cfg)
        self.assertEqual(set(labels.values()), {"vertical"})

    def test_risk_reversal_is_directional(self):
        trades = [
            trade(105, Right.CALL, 2.00, 500, 0, bid=1.90, ask=2.00),    # bought call
            trade(95, Right.PUT, 1.50, 500, 50, bid=1.50, ask=1.60),     # sold put
        ]
        labels = fch.detect_structures(trades, self.cfg)
        self.assertEqual(set(labels.values()), {"risk_reversal"})

    def test_unrelated_trades_are_not_paired(self):
        trades = [
            trade(100, Right.CALL, 3.00, 500, 0),
            trade(100, Right.PUT, 2.80, 90, 50),        # very different size
        ]
        self.assertEqual(fch.detect_structures(trades, self.cfg), {})


class SweepTest(unittest.TestCase):
    def setUp(self):
        self.cfg = OptionsFlowConfig()

    def test_multi_exchange_burst_is_a_sweep(self):
        trades = [
            trade(102, Right.CALL, 2.10, 500, i * 80, e, bid=2.00, ask=2.10)
            for i, e in enumerate(["CBOE", "ISE", "PHLX", "BOX"])
        ]
        self.assertEqual(len(fch.detect_sweeps(trades, self.cfg)), 4)

    def test_single_exchange_is_not_a_sweep(self):
        trades = [
            trade(102, Right.CALL, 2.10, 500, i * 80, "CBOE", bid=2.00, ask=2.10)
            for i in range(4)
        ]
        self.assertEqual(fch.detect_sweeps(trades, self.cfg), set())

    def test_slow_sequence_is_not_a_sweep(self):
        trades = [
            trade(102, Right.CALL, 2.10, 500, i * 5000, e, bid=2.00, ask=2.10)
            for i, e in enumerate(["CBOE", "ISE", "PHLX", "BOX"])
        ]
        self.assertEqual(fch.detect_sweeps(trades, self.cfg), set())


class FlowScoringTest(unittest.TestCase):
    def setUp(self):
        self.cfg = OptionsFlowConfig()

    def _sweep(self, right=Right.CALL, strike=102):
        return [
            trade(strike, right, 2.10, 2000, i * 80, e, bid=2.00, ask=2.10)
            for i, e in enumerate(["CBOE", "ISE", "PHLX", "BOX"])
        ]

    def test_call_buying_is_bullish_and_put_buying_bearish(self):
        bull = fch.evaluate("ACME", self._sweep(Right.CALL, 102), NOW, self.cfg)
        bear = fch.evaluate("ACME", self._sweep(Right.PUT, 98), NOW, self.cfg)
        self.assertGreater(bull[0].score, 0)
        self.assertLess(bear[0].score, 0)

    def test_balanced_flow_produces_no_signal(self):
        trades = self._sweep(Right.CALL, 102) + self._sweep(Right.PUT, 98)
        got = fch.evaluate("ACME", trades, NOW, self.cfg)
        self.assertTrue(not got or abs(got[0].score) < 0.2)

    def test_scoring_is_relative_to_the_symbols_own_history(self):
        """The identical tape is a non-event in a busy name and a signal in a
        quiet one. Absolute premium alone would just detect large caps."""
        rng = random.Random(11)
        trades = self._sweep()

        busy = fch.FlowBaseline()
        for _ in range(30):
            busy.record("ACME", rng.gauss(20_000_000, 4_000_000))

        quiet = fch.FlowBaseline()
        for _ in range(30):
            quiet.record("ACME", rng.gauss(300_000, 80_000))

        self.assertEqual(fch.evaluate("ACME", trades, NOW, self.cfg, busy), [])
        self.assertTrue(fch.evaluate("ACME", trades, NOW, self.cfg, quiet))

    def test_signable_fraction_never_exceeds_one(self):
        s = fch.summarise("ACME", self._sweep(), NOW, self.cfg)
        self.assertLessEqual(s.signable_fraction, 1.0)

    def test_far_otm_lottery_tickets_are_filtered(self):
        far = [
            trade(150, Right.CALL, 0.30, 5000, i * 80, e, bid=0.28, ask=0.30, spot=100.0)
            for i, e in enumerate(["CBOE", "ISE", "PHLX", "BOX"])
        ]
        self.assertEqual(fch.evaluate("ACME", far, NOW, self.cfg), [])

    def test_unsignable_prints_are_excluded(self):
        mids = [
            trade(102, Right.CALL, 2.05, 2000, i * 80, e, bid=2.00, ask=2.10)
            for i, e in enumerate(["CBOE", "ISE", "PHLX", "BOX"])
        ]
        s = fch.summarise("ACME", mids, NOW, self.cfg)
        self.assertEqual(s.kept_count, 0)


# ─────────────────────────────────────────────────────────────────────────────
# Earnings
# ─────────────────────────────────────────────────────────────────────────────

class EarningsTest(unittest.TestCase):
    def setUp(self):
        self.cfg = EarningsConfig()

    def _event(self, days_ahead=1, eps_est=2.0, eps_act=None, released=None):
        report = (NOW + timedelta(days=days_ahead)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return EarningsEvent(
            symbol="ACME", report_date=report, session="amc",
            scheduled_known_at=NOW - timedelta(days=14),
            eps_estimate=eps_est, eps_actual=eps_act, released_at=released,
        )

    def test_blackout_before_a_scheduled_report(self):
        ev = self._event(days_ahead=0)
        state = ech.blackout("ACME", [ev], NOW, self.cfg)
        self.assertTrue(state.active)
        self.assertIn("earnings in", state.reason)

    def test_no_blackout_when_the_report_is_far_off(self):
        ev = self._event(days_ahead=10)
        self.assertFalse(ech.blackout("ACME", [ev], NOW, self.cfg).active)

    def test_unpublished_schedule_cannot_cause_a_blackout(self):
        ev = EarningsEvent(
            symbol="ACME",
            report_date=NOW.replace(hour=0, minute=0, second=0, microsecond=0),
            session="amc",
            scheduled_known_at=NOW + timedelta(days=1),   # announced tomorrow
            eps_estimate=2.0,
        )
        self.assertFalse(ech.blackout("ACME", [ev], NOW, self.cfg).active)

    def test_beat_scores_positive_and_miss_negative(self):
        released = NOW - timedelta(hours=2)
        beat = self._event(-1, 2.0, 2.40, released)
        miss = self._event(-1, 2.0, 1.60, released)
        self.assertGreater(ech.evaluate("ACME", [beat], NOW, self.cfg)[0].score, 0)
        self.assertLess(ech.evaluate("ACME", [miss], NOW, self.cfg)[0].score, 0)

    def test_tape_overrides_consensus_when_they_conflict(self):
        """A beat that the market sold is not a bullish signal — the market
        read the whole release and the EPS line did not."""
        released = NOW - timedelta(hours=2)
        beat = self._event(-1, 2.0, 2.40, released)
        got = ech.evaluate("ACME", [beat], NOW, self.cfg, reaction_pct=-5.0)[0]
        self.assertLess(got.score, 0)
        self.assertEqual(got.detail["agreement"], "conflict")

    def test_agreement_raises_confidence(self):
        released = NOW - timedelta(hours=2)
        beat = self._event(-1, 2.0, 2.40, released)
        alone = ech.evaluate("ACME", [beat], NOW, self.cfg)[0]
        aligned = ech.evaluate("ACME", [beat], NOW, self.cfg, reaction_pct=4.0)[0]
        self.assertGreater(aligned.confidence, alone.confidence)
        self.assertEqual(aligned.detail["agreement"], "aligned")

    def test_near_zero_estimate_does_not_explode(self):
        released = NOW - timedelta(hours=2)
        ev = self._event(-1, 0.001, 0.011, released)
        self.assertIsNone(ev.eps_surprise_pct())


# ─────────────────────────────────────────────────────────────────────────────
# Market
# ─────────────────────────────────────────────────────────────────────────────

class BarSeriesTest(unittest.TestCase):
    """The engine re-fetches an overlapping window every cycle, so the series
    must de-duplicate against its whole history — not just the newest bar."""

    def _bars(self, n=5):
        return [
            Bar("X", NOW + timedelta(minutes=5 * i), NOW + timedelta(minutes=5 * (i + 1)),
                100, 101, 99, 100, 1000)
            for i in range(n)
        ]

    def test_refetching_history_does_not_duplicate(self):
        s = mch.BarSeries("X")
        for _ in range(3):
            for b in self._bars():
                s.append(b)
        self.assertEqual(len(s), 5)
        # Volume inflation is the damaging consequence: it feeds RVOL directly.
        self.assertEqual(sum(b.volume for b in s), 5000)

    def test_out_of_order_arrival_is_sorted(self):
        s = mch.BarSeries("X")
        for b in reversed(self._bars()):
            s.append(b)
        self.assertTrue(all(s[i].end < s[i + 1].end for i in range(len(s) - 1)))

    def test_resent_bar_replaces_rather_than_stacks(self):
        s = mch.BarSeries("X")
        for b in self._bars():
            s.append(b)
        settled = Bar("X", NOW + timedelta(minutes=10), NOW + timedelta(minutes=15),
                      100, 105, 95, 102, 7777)
        s.append(settled)
        self.assertEqual(len(s), 5)
        self.assertEqual(s[2].volume, 7777)

    def test_trim_keeps_the_index_consistent(self):
        s = mch.BarSeries("X")
        for b in self._bars(20):
            s.append(b)
        s.trim(5)
        self.assertEqual(len(s), 5)
        # Re-appending a kept bar must still replace, not duplicate.
        s.append(s[-1])
        self.assertEqual(len(s), 5)


class MarketTest(unittest.TestCase):
    def test_atr_needs_enough_history(self):
        bars = [
            Bar("X", NOW + timedelta(minutes=5 * i), NOW + timedelta(minutes=5 * (i + 1)),
                100, 101, 99, 100, 1000)
            for i in range(5)
        ]
        self.assertIsNone(mch.atr(bars, 14))

    def test_atr_is_positive_on_real_ranges(self):
        bars = [
            Bar("X", NOW + timedelta(minutes=5 * i), NOW + timedelta(minutes=5 * (i + 1)),
                100 + i, 101 + i, 99 + i, 100 + i, 1000)
            for i in range(30)
        ]
        val = mch.atr(bars, 14)
        self.assertIsNotNone(val)
        self.assertGreater(val, 0)

    def test_rvol_corrects_for_time_of_day(self):
        """400k shares by 09:45 is extraordinary; the same by 15:45 is a quiet
        day. Without the elapsed-fraction correction both read the same."""
        early = mch.relative_volume(400_000, 4_000_000, 0.05)
        late = mch.relative_volume(400_000, 4_000_000, 0.95)
        self.assertGreater(early, late)
        self.assertGreater(early, 1.0)
        self.assertLess(late, 1.0)

    def test_liquidity_filter_rejects_thin_names(self):
        s = Settings.load()
        snap = mch.MarketSnapshot(
            symbol="THIN", now=NOW, last=50.0, atr=1.0, atr_pct=2.0, vwap=50.0,
            ema_fast=50.0, ema_slow=50.0, rvol=1.0, gap_pct=0.0, prev_close=50.0,
            session_high=51.0, session_low=49.0, adv_dollar=1_000_000.0,
            bar_count=100, last_bar_end=NOW,
        )
        ok, why = mch.is_tradeable(snap, s.universe)
        self.assertFalse(ok)
        self.assertIn("ADV", why)

    def test_liquidity_filter_rejects_untradeably_wide_spreads(self):
        s = Settings.load()
        snap = mch.MarketSnapshot(
            symbol="WIDE", now=NOW, last=50.0, atr=1.0, atr_pct=2.0, vwap=50.0,
            ema_fast=50.0, ema_slow=50.0, rvol=1.0, gap_pct=0.0, prev_close=50.0,
            session_high=51.0, session_low=49.0, adv_dollar=50_000_000.0,
            bar_count=100, last_bar_end=NOW,
        )
        ok, why = mch.is_tradeable(snap, s.universe, spread_bps=45.0)
        self.assertFalse(ok)
        self.assertIn("spread", why)


if __name__ == "__main__":
    unittest.main()
