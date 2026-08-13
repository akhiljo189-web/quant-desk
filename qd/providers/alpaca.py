"""
qd.providers.alpaca — broker adapter (paper and live).

Implements Broker. Two properties matter more than the API surface:

BRACKETS ARE ATOMIC
    Entry, stop and target are submitted as one bracket order, so the
    protective stop exists at the broker from the instant the position does. A
    stop enforced by this process is not a stop — it is an intention that
    survives exactly as long as the process, the network, and the machine do.
    Positions outlive all three. Every order this adapter sends carries a
    broker-side stop; there is no path through `submit()` that opens naked risk.

THE GATE IS CHECKED PER ORDER
    `gate.enforce()` runs before every live submission, not once at startup. A
    process that passed the gate in March must not still be trading on that
    verdict in September, when the proof has expired and the paper record has
    moved on.

Idempotency comes from `client_order_id`, derived deterministically from the
intent. A restart that re-derives the same intent reuses the same ID and the
broker rejects the duplicate, which is the difference between a crash costing
nothing and a crash silently doubling a position.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional, Sequence

from qd.config import Mode, ProviderConfig, Settings
from qd.gate import PaperRecord, Requirements, enforce
from qd.providers.base import AccountInfo, BrokerOrder, ProviderError
from qd.providers.http import HttpClient, RateLimiter, Recorder
from qd.types import Order, Position, Side, ensure_utc

logger = logging.getLogger(__name__)


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


class AlpacaBroker:
    """Alpaca trading adapter."""

    def __init__(
        self,
        settings: Settings,
        paper_record: Optional[PaperRecord] = None,
        requirements: Optional[Requirements] = None,
    ) -> None:
        cfg: ProviderConfig = settings.providers
        if not cfg.alpaca_key_id or not cfg.alpaca_secret:
            raise ProviderError("ALPACA_KEY_ID / ALPACA_SECRET_KEY are not set")

        self.settings = settings
        self.mode = settings.mode
        self.paper_record = paper_record
        self.requirements = requirements

        base = cfg.alpaca_base(self.mode)
        self.http = HttpClient(
            base_url=base,
            headers={
                "APCA-API-KEY-ID": cfg.alpaca_key_id,
                "APCA-API-SECRET-KEY": cfg.alpaca_secret,
                "Content-Type": "application/json",
            },
            timeout=cfg.request_timeout,
            max_retries=cfg.max_retries,
            rate_limiter=RateLimiter(cfg.rate_limit_per_min),
            recorder=Recorder(cfg.cache_dir, cfg.record_responses),
        )
        logger.info(
            "Alpaca adapter ready (%s endpoint)",
            "LIVE" if self.mode is Mode.LIVE else "paper",
        )

    # ── account ──────────────────────────────────────────────────────────────

    def account(self) -> AccountInfo:
        r = self.http.get("/v2/account", tag="account") or {}
        return AccountInfo(
            equity=float(r.get("equity", 0.0)),
            cash=float(r.get("cash", 0.0)),
            buying_power=float(r.get("buying_power", 0.0)),
            currency=r.get("currency", "USD"),
            pattern_day_trader=bool(r.get("pattern_day_trader", False)),
            trading_blocked=bool(r.get("trading_blocked", False)),
            account_id=str(r.get("account_number", "")),
        )

    def positions(self) -> list[Position]:
        """Positions as the broker sees them.

        This is the authority, not local state. On restart the broker's view is
        the truth and anything this process believed is a stale guess.
        """
        rows = self.http.get("/v2/positions", tag="positions") or []
        out: list[Position] = []
        for r in rows:
            qty = float(r.get("qty", 0.0))
            side = Side.BUY if qty >= 0 else Side.SELL
            entry = float(r.get("avg_entry_price", 0.0))
            out.append(Position(
                symbol=r["symbol"], side=side, quantity=abs(qty),
                entry_price=entry,
                # The real stop lives in the open bracket leg; reconciliation
                # fills these in from open orders.
                stop_price=entry, target_price=entry,
                opened_at=ensure_utc(datetime.now().astimezone()),
            ))
        return out

    def open_orders(self) -> list[BrokerOrder]:
        rows = self.http.get(
            "/v2/orders", {"status": "open", "limit": 500, "nested": "true"},
            tag="orders",
        ) or []
        return [self._to_order(r) for r in rows]

    @staticmethod
    def _to_order(r: dict) -> BrokerOrder:
        return BrokerOrder(
            id=str(r.get("id", "")),
            client_order_id=str(r.get("client_order_id", "")),
            symbol=r.get("symbol", ""),
            side=Side.BUY if r.get("side") == "buy" else Side.SELL,
            quantity=float(r.get("qty") or 0.0),
            status=str(r.get("status", "")),
            filled_quantity=float(r.get("filled_qty") or 0.0),
            filled_avg_price=(
                float(r["filled_avg_price"]) if r.get("filled_avg_price") else None
            ),
            submitted_at=_dt(r.get("submitted_at")),
            order_type=str(r.get("type", "market")),
            limit_price=float(r["limit_price"]) if r.get("limit_price") else None,
        )

    # ── orders ───────────────────────────────────────────────────────────────

    def submit(self, order: Order) -> BrokerOrder:
        """Submit a bracket order: entry, protective stop and target together."""
        if self.mode is Mode.LIVE:
            # Per-order, deliberately. See the module docstring.
            enforce(self.settings, self.paper_record,
                    self.requirements or Requirements())

        if order.quantity <= 0:
            raise ProviderError(f"{order.symbol}: non-positive quantity")

        body: dict[str, Any] = {
            "symbol": order.symbol,
            "qty": str(int(order.quantity)),
            "side": order.side.value,
            "time_in_force": "day",
            "order_class": "bracket",
            "client_order_id": order.client_order_id,
            "take_profit": {"limit_price": f"{order.target_price:.2f}"},
            "stop_loss": {"stop_price": f"{order.stop_price:.2f}"},
        }
        if order.limit_price is not None:
            # Marketable limit rather than a plain market order. A market order
            # in a thin book is an invitation to be filled at the worst
            # available print, and the loss is silent — it shows up as
            # "slippage" rather than as an error.
            body["type"] = "limit"
            body["limit_price"] = f"{order.limit_price:.2f}"
        else:
            body["type"] = "market"

        try:
            r = self.http.post("/v2/orders", body, tag="submit")
        except ProviderError as exc:
            if "client_order_id" in str(exc) and "exist" in str(exc).lower():
                # The idempotency guard doing its job after a restart.
                logger.warning(
                    "%s: duplicate client_order_id %s — already submitted, not resending",
                    order.symbol, order.client_order_id,
                )
                existing = self.find_by_client_id(order.client_order_id)
                if existing:
                    return existing
            raise

        if not r:
            raise ProviderError(f"{order.symbol}: empty response from order submit")

        bo = self._to_order(r)
        logger.info(
            "SUBMIT %s %s qty=%g stop=%.2f target=%.2f id=%s",
            order.symbol, order.side.value, order.quantity,
            order.stop_price, order.target_price, bo.id,
        )
        return bo

    def find_by_client_id(self, client_order_id: str) -> Optional[BrokerOrder]:
        r = self.http.get(
            "/v2/orders:by_client_order_id",
            {"client_order_id": client_order_id}, tag="order-lookup",
        )
        return self._to_order(r) if r else None

    def cancel(self, order_id: str) -> bool:
        try:
            self.http.delete(f"/v2/orders/{order_id}", tag="cancel")
            return True
        except ProviderError as exc:
            logger.warning("cancel %s failed: %s", order_id, exc)
            return False

    def close_position(
        self, symbol: str, quantity: Optional[float] = None
    ) -> Optional[BrokerOrder]:
        params = f"?qty={int(quantity)}" if quantity else ""
        try:
            r = self.http.delete(f"/v2/positions/{symbol}{params}", tag="close")
            return self._to_order(r) if r else None
        except ProviderError as exc:
            logger.error("close %s failed: %s", symbol, exc)
            return None

    def replace_stop(self, symbol: str, new_stop: float) -> bool:
        """Move the protective stop by replacing the bracket's stop leg."""
        for o in self.open_orders():
            if o.symbol != symbol or o.order_type not in ("stop", "stop_limit"):
                continue
            try:
                self.http.patch(
                    f"/v2/orders/{o.id}", {"stop_price": f"{new_stop:.2f}"},
                    tag="replace-stop",
                )
                logger.info("%s: stop moved to %.2f", symbol, new_stop)
                return True
            except ProviderError as exc:
                logger.error("%s: could not move stop: %s", symbol, exc)
                return False
        logger.warning("%s: no stop leg found to replace", symbol)
        return False

    # ── reconciliation ───────────────────────────────────────────────────────

    def reconcile(self) -> tuple[list[Position], list[str]]:
        """Rebuild true state from the broker and report anything unprotected.

        Run at startup. The dangerous post-restart state is a position whose
        bracket legs were cancelled or never attached — it looks like an
        ordinary holding and has no stop behind it.
        """
        positions = self.positions()
        orders = self.open_orders()
        stops = {
            o.symbol for o in orders if o.order_type in ("stop", "stop_limit")
        }
        naked = [p.symbol for p in positions if p.symbol not in stops]

        for p in positions:
            for o in orders:
                if o.symbol == p.symbol and o.order_type in ("stop", "stop_limit"):
                    p.stop_price = o.limit_price or p.stop_price
        if naked:
            logger.error(
                "RECONCILE: %d position(s) with no protective stop: %s",
                len(naked), ", ".join(naked),
            )
        return positions, naked


__all__ = ["AlpacaBroker"]
