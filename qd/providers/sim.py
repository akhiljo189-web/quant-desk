"""
qd.providers.sim — a simulated broker implementing the Broker protocol.

Used by replay, and usable as a local paper broker. Everything here exists to
make simulated fills PESSIMISTIC, because the ways a simulator can flatter a
strategy are numerous, subtle, and all point the same direction.

Three rules, each closing a specific hole:

  NEXT-BAR-OPEN FILLS
      An order decided on a bar's close fills at the NEXT bar's open. Filling at
      the signal bar's close means trading on a price that, at the moment of
      decision, had not happened yet. This single error is enough to make a
      random strategy look profitable, because the close you are filling at is
      the same close that generated the signal.

  THE ORDERING BAND
      When one bar's range contains both the stop and the target, OHLC cannot
      say which came first. Assuming the target is the most expensive
      assumption available. This simulator assumes the STOP by default and can
      report both orderings so the true result is bracketed rather than guessed.

  COSTS ON BOTH SIDES
      Half the spread plus slippage, entering and exiting, plus fees. Retail
      equities are commission-free and are not cost-free; the spread is the
      commission, and it is charged twice per round trip.

None of this makes the simulation accurate. It makes it biased against the
strategy, which is the only bias that is safe to have.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Optional, Sequence

from qd.config import ExecutionConfig
from qd.providers.base import AccountInfo, BrokerOrder, ProviderError
from qd.types import Bar, Fill, Order, Position, Quote, Side, ensure_utc

logger = logging.getLogger(__name__)

Ordering = Literal["worst", "optimistic", "neutral"]


@dataclass
class _Pending:
    order: Order
    submitted_at: datetime
    broker_order: BrokerOrder


@dataclass
class SimFillEvent:
    """Recorded for post-hoc analysis of how fills actually behaved."""
    symbol: str
    side: Side
    quantity: float
    price: float
    ts: datetime
    kind: str            # entry | stop | target | time | eod | manual
    slippage: float = 0.0
    cost: float = 0.0
    ambiguous_bar: bool = False   # bar contained both stop and target


class SimBroker:
    """Deterministic simulated broker."""

    def __init__(
        self,
        equity: float,
        cfg: ExecutionConfig,
        cost_mult: float = 1.0,
        ordering: Ordering = "worst",
        spread_bps: float = 3.0,
    ) -> None:
        self.cfg = cfg
        self.cost_mult = cost_mult
        self.ordering = ordering
        self.default_spread_bps = spread_bps

        self._equity = equity
        self._cash = equity
        self._positions: dict[str, Position] = {}
        self._pending: dict[str, _Pending] = {}
        self._seq = 0
        self.fills: list[SimFillEvent] = []
        self.ambiguous_bars = 0

    # ── Broker protocol ──────────────────────────────────────────────────────

    def account(self) -> AccountInfo:
        return AccountInfo(
            equity=self._equity, cash=self._cash,
            buying_power=self._cash * 2, account_id="SIM",
        )

    def positions(self) -> list[Position]:
        return list(self._positions.values())

    def open_orders(self) -> list[BrokerOrder]:
        return [p.broker_order for p in self._pending.values()]

    def submit(self, order: Order) -> BrokerOrder:
        if order.symbol in self._positions:
            raise ProviderError(f"{order.symbol}: position already open")
        if order.client_order_id in {p.order.client_order_id for p in self._pending.values()}:
            # Idempotency: a re-derived intent after a restart must not double up.
            raise ProviderError(f"duplicate client_order_id {order.client_order_id}")

        self._seq += 1
        bo = BrokerOrder(
            id=f"sim-{self._seq}",
            client_order_id=order.client_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            status="accepted",
            order_type="limit" if order.limit_price else "market",
            limit_price=order.limit_price,
        )
        self._pending[bo.id] = _Pending(order, ensure_utc(datetime.now().astimezone()), bo)
        return bo

    def cancel(self, order_id: str) -> bool:
        return self._pending.pop(order_id, None) is not None

    def close_position(
        self, symbol: str, quantity: Optional[float] = None
    ) -> Optional[BrokerOrder]:
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        pos.stop_price = pos.stop_price      # no-op; closure happens in on_bar
        self._flag_close(symbol, quantity)
        return None

    def replace_stop(self, symbol: str, new_stop: float) -> bool:
        pos = self._positions.get(symbol)
        if pos is None:
            return False
        pos.stop_price = new_stop
        return True

    # ── simulation ───────────────────────────────────────────────────────────

    _close_requests: dict[str, Optional[float]]

    def _flag_close(self, symbol: str, quantity: Optional[float]) -> None:
        if not hasattr(self, "_close_requests"):
            self._close_requests = {}
        self._close_requests[symbol] = quantity

    def _spread(self, price: float) -> float:
        return price * self.default_spread_bps / 10_000.0

    def _entry_cost(self, price: float, side: Side) -> float:
        """Half-spread against us, plus modelled slippage."""
        half = self._spread(price) / 2.0
        slip = price * self.cfg.slippage_bps / 10_000.0
        total = (half + slip) * self.cost_mult
        return price + total if side is Side.BUY else price - total

    def _exit_cost(self, price: float, side: Side) -> float:
        half = self._spread(price) / 2.0
        slip = price * self.cfg.slippage_bps / 10_000.0
        total = (half + slip) * self.cost_mult
        # Exiting a long means selling, so the cost pushes the price down.
        return price - total if side is Side.BUY else price + total

    def _fees(self, price: float, qty: float, side: Side) -> float:
        fees = qty * self.cfg.commission_per_share
        if side is Side.SELL:                      # regulatory fees are sell-side
            fees += price * qty * self.cfg.sec_fee_rate
        fees += qty * self.cfg.finra_taf_per_share
        return fees

    def on_bar(self, symbol: str, bar: Bar) -> list[SimFillEvent]:
        """Advance the simulation for one symbol by one bar.

        Order matters: pending entries fill at this bar's OPEN, and only then is
        the same bar checked for stop/target. An entry filled at the open is
        legitimately exposed to that bar's range.
        """
        events: list[SimFillEvent] = []
        events.extend(self._fill_pending(symbol, bar))
        events.extend(self._manage_open(symbol, bar))
        self.fills.extend(events)
        return events

    def _fill_pending(self, symbol: str, bar: Bar) -> list[SimFillEvent]:
        out: list[SimFillEvent] = []
        for oid, pend in list(self._pending.items()):
            if pend.order.symbol != symbol:
                continue
            o = pend.order

            fill_px = self._entry_cost(bar.open, o.side)

            # A marketable limit does not fill if the open gapped through it.
            # Modelling this matters: gap days are exactly when a strategy
            # thinks it got filled at a good price and in reality got nothing.
            if o.limit_price is not None:
                if o.side is Side.BUY and fill_px > o.limit_price:
                    continue
                if o.side is Side.SELL and fill_px < o.limit_price:
                    continue

            fees = self._fees(fill_px, o.quantity, o.side)
            self._positions[symbol] = Position(
                symbol=symbol, side=o.side, quantity=o.quantity,
                entry_price=fill_px, stop_price=o.stop_price,
                target_price=o.target_price, opened_at=bar.end,
                intent=o.intent,
            )
            self._cash -= fees
            self._equity -= fees
            del self._pending[oid]
            out.append(SimFillEvent(
                symbol=symbol, side=o.side, quantity=o.quantity, price=fill_px,
                ts=bar.end, kind="entry", slippage=abs(fill_px - bar.open), cost=fees,
            ))
        return out

    def _manage_open(self, symbol: str, bar: Bar) -> list[SimFillEvent]:
        pos = self._positions.get(symbol)
        if pos is None:
            return []

        requested = getattr(self, "_close_requests", {}).pop(symbol, "none")
        if requested != "none":
            return [self._exit(pos, bar.close, bar.end, "manual")]

        long = pos.side is Side.BUY
        hit_stop = bar.low <= pos.stop_price if long else bar.high >= pos.stop_price
        hit_target = bar.high >= pos.target_price if long else bar.low <= pos.target_price

        if hit_stop and hit_target:
            # Both inside one bar. OHLC cannot resolve the order, so pick by
            # policy rather than by hope.
            self.ambiguous_bars += 1
            if self.ordering == "optimistic":
                return [self._exit(pos, pos.target_price, bar.end, "target", True)]
            if self.ordering == "neutral":
                mid = (pos.stop_price + pos.target_price) / 2.0
                return [self._exit(pos, mid, bar.end, "ambiguous", True)]
            return [self._exit(pos, pos.stop_price, bar.end, "stop", True)]

        if hit_stop:
            # A gap through the stop fills at the open, not the stop price. This
            # is where "1R risk" stops being true, and it is the whole reason
            # overnight exposure is capped separately.
            px = bar.open if (long and bar.open < pos.stop_price) or (
                not long and bar.open > pos.stop_price) else pos.stop_price
            return [self._exit(pos, px, bar.end, "stop")]

        if hit_target:
            px = bar.open if (long and bar.open > pos.target_price) or (
                not long and bar.open < pos.target_price) else pos.target_price
            return [self._exit(pos, px, bar.end, "target")]

        return []

    def _exit(
        self, pos: Position, raw_price: float, ts: datetime, kind: str,
        ambiguous: bool = False,
    ) -> SimFillEvent:
        px = self._exit_cost(raw_price, pos.side)
        fees = self._fees(px, pos.quantity, pos.side.opposite)
        pnl = pos.unrealized(px) - fees
        self._cash += pnl
        self._equity += pnl
        del self._positions[pos.symbol]
        return SimFillEvent(
            symbol=pos.symbol, side=pos.side.opposite, quantity=pos.quantity,
            price=px, ts=ts, kind=kind, slippage=abs(px - raw_price),
            cost=fees, ambiguous_bar=ambiguous,
        )

    def force_close(self, symbol: str, price: float, ts: datetime, kind: str = "eod") -> Optional[SimFillEvent]:
        pos = self._positions.get(symbol)
        if pos is None:
            return None
        ev = self._exit(pos, price, ts, kind)
        self.fills.append(ev)
        return ev

    # ── reporting ────────────────────────────────────────────────────────────

    @property
    def equity(self) -> float:
        return self._equity

    def mark_to_market(self, marks: dict[str, float]) -> float:
        unreal = sum(
            p.unrealized(marks[p.symbol]) for p in self._positions.values()
            if p.symbol in marks
        )
        return self._equity + unreal


__all__ = ["SimBroker", "SimFillEvent", "Ordering"]
