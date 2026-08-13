"""
The pre-trade spread gate.

`max_spread_bps` sat in the config and in RISK.md while `is_tradeable()` was
only ever called with `spread_bps=None` — a documented protection that had
never once fired. These tests exist so it cannot quietly revert to that.

Two behaviours matter equally:

  a wide quote must BLOCK the order
  an unavailable quote must be RECORDED, not silently treated as a pass

The second is the subtle one. Entry-level data plans exclude quote data, and a
gate that succeeds whenever it cannot measure anything is indistinguishable
from no gate at all.
"""

from __future__ import annotations

import dataclasses
import os
import unittest
from datetime import datetime, timedelta

from qd.config import Mode, Settings
from qd.engine import Engine, SymbolState
from qd.features.market import BarSeries, MarketSnapshot
from qd.journal import Journal
from qd.portfolio import Portfolio
from qd.providers.base import Providers
from qd.types import UTC, Evidence, Quote, Source

NOW = datetime(2026, 3, 10, 16, 0, tzinfo=UTC)      # 12:00 ET, mid-session


class _Broker:
    def __init__(self):
        self.submitted = []

    def account(self): raise NotImplementedError
    def positions(self): return []
    def open_orders(self): return []
    def cancel(self, oid): return False
    def close_position(self, sym, qty=None): return None
    def replace_stop(self, sym, px): return False

    def submit(self, order):
        self.submitted.append(order)
        return dataclasses.replace(
            __import__("qd.providers.base", fromlist=["BrokerOrder"]).BrokerOrder(
                id="x", client_order_id=order.client_order_id, symbol=order.symbol,
                side=order.side, quantity=order.quantity, status="accepted",
            )
        )


class _Market:
    """Market provider whose quote behaviour is configurable."""

    def __init__(self, quote_result):
        self._quote = quote_result
        self.quote_calls = 0

    def bars(self, *a, **k): return []
    def daily_bars(self, *a, **k): return []

    def quote(self, symbol, at=None):
        self.quote_calls += 1
        if isinstance(self._quote, Exception):
            raise self._quote
        return self._quote


def build(quote_result, journal_path=None):
    # A unique journal per engine. The journal is append-only by design, so a
    # shared path makes every test count the previous tests' records too.
    import tempfile, uuid
    journal_path = journal_path or os.path.join(
        tempfile.gettempdir(), f"qd-spread-{uuid.uuid4().hex}.jsonl"
    )
    s = Settings.load(Mode.REPLAY)
    s = dataclasses.replace(
        s,
        universe=dataclasses.replace(s.universe, symbols=("NVDA",)),
        # Regime is exercised in its own tests; disable it so these focus on
        # the spread gate rather than on warm-up history.
        context=dataclasses.replace(s.context, enabled=False),
    )
    market, broker = _Market(quote_result), _Broker()
    eng = Engine(
        s, Providers(market=market, broker=broker),
        Portfolio(100_000, s.risk, s.universe),
        Journal(journal_path),
    )
    st = SymbolState("NVDA", BarSeries("NVDA"), BarSeries("NVDA"))
    st.snapshot = MarketSnapshot(
        symbol="NVDA", now=NOW, last=100.0, atr=2.0, atr_pct=2.0, vwap=100.0,
        ema_fast=100.0, ema_slow=100.0, rvol=1.5, gap_pct=0.0, prev_close=100.0,
        session_high=101.0, session_low=99.0, adv_dollar=50_000_000.0,
        bar_count=120, last_bar_end=NOW,
    )
    # Trigger + confirmation, so the candidate reaches the spread gate.
    st.evidence = [
        Evidence(source=Source.EARNINGS, kind="pead", symbol="NVDA",
                 score=0.8, confidence=0.9, observed_at=NOW, ttl=timedelta(hours=6)),
        Evidence(source=Source.OPTIONS_FLOW, kind="premium_imbalance", symbol="NVDA",
                 score=0.7, confidence=0.9, observed_at=NOW, ttl=timedelta(hours=1)),
    ]
    eng.states = {"NVDA": st}
    return eng, market, broker


def quote(bid: float, ask: float) -> Quote:
    return Quote("NVDA", NOW, bid, ask, 100.0, 100.0)


class SpreadGateTest(unittest.TestCase):
    def _consider(self, eng):
        from qd.engine import CycleReport
        from qd.clock import CALENDAR
        report = CycleReport(now=NOW, phase=CALENDAR.phase(NOW))
        eng.consider("NVDA", NOW, report)
        return report

    def test_tight_spread_allows_the_order(self):
        # 100.00 / 100.02 = 2bps, well inside the 25bps cap.
        eng, market, broker = build(quote(100.00, 100.02))
        self._consider(eng)
        self.assertEqual(len(broker.submitted), 1)
        self.assertEqual(market.quote_calls, 1)

    def test_wide_spread_blocks_the_order(self):
        """THE regression test: before the gate was wired up, this traded."""
        # 100.00 / 100.60 = ~60bps, far outside the cap.
        eng, market, broker = build(quote(100.00, 100.60))
        self._consider(eng)
        self.assertEqual(broker.submitted, [])

        blocked = [
            r for r in eng.journal.read(["assessment"])
            if not r.get("taken") and "spread" in (r.get("blocked") or "")
        ]
        self.assertTrue(blocked, "the refusal must be journalled with its reason")

    def test_missing_quote_is_recorded_not_silently_passed(self):
        """On a plan without quote data the spread is UNMEASURABLE. The trade
        proceeds — blocking every trade forever would be worse — but the gap is
        stated once, so nobody later believes spreads were being checked."""
        eng, market, broker = build(None)
        self._consider(eng)
        self.assertEqual(len(broker.submitted), 1)
        events = [
            r for r in eng.journal.read(["event"])
            if "spread checks unavailable" in (r.get("message") or "")
        ]
        self.assertEqual(len(events), 1)

    def test_the_unavailable_warning_is_emitted_only_once(self):
        eng, market, broker = build(None)
        self._consider(eng)
        eng.portfolio.close("NVDA", 100.0, NOW, "test")
        self._consider(eng)
        events = [
            r for r in eng.journal.read(["event"])
            if "spread checks unavailable" in (r.get("message") or "")
        ]
        self.assertEqual(len(events), 1, "a standing plan property, not a per-decision event")

    def test_a_provider_error_does_not_kill_the_cycle(self):
        from qd.providers.base import ProviderError
        eng, market, broker = build(ProviderError("403 not entitled"))
        report = self._consider(eng)
        self.assertEqual(report.errors, [])
        self.assertEqual(len(broker.submitted), 1)

    def test_crossed_quote_is_treated_as_no_quote(self):
        """bid above ask is a broken or stale quote; never trade on one."""
        eng, market, broker = build(quote(100.50, 100.00))
        self._consider(eng)
        events = [
            r for r in eng.journal.read(["event"])
            if "spread checks unavailable" in (r.get("message") or "")
        ]
        self.assertEqual(len(events), 1)


if __name__ == "__main__":
    unittest.main()
