"""
qd.risk — position sizing and the limits that bind unconditionally.

Sizing is RISK-FIRST. Quantity is derived from the stop distance and the cash a
single loss may cost:

    quantity = (equity x risk_pct) / |entry - stop|

Never from a notional target, never from conviction. A high-conviction signal
gets the same cash risk as a marginal one, because conviction is an opinion and
the stop distance is a measurement. Sizing up on conviction is how one confident
wrong idea does the damage of five ordinary ones — and confidence is not
correlated with being right in any way this system can verify.

Every cap below can only REDUCE the risk-derived size. None can raise it. The
asymmetry is the whole design: caps are protection against being wrong about
correlation or liquidity, and protection that can be argued upward is not
protection.

If a cap cuts the size below what is tradeable, the trade is REJECTED rather
than squeezed in. A position too small to matter still pays full costs and
still occupies a slot, an attention budget, and a share of the day's risk.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Sequence

from qd.clock import CALENDAR, MarketCalendar
from qd.config import RiskConfig, UniverseConfig
from qd.portfolio import Portfolio
from qd.types import Intent, Side, ensure_utc

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Check:
    """One gate and its outcome — recorded whether it passed or failed, so the
    journal shows the full set of constraints a trade was measured against."""
    name: str
    passed: bool
    detail: str = ""


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    quantity: float = 0.0
    cash_risk: float = 0.0
    notional: float = 0.0
    reason: str = ""
    capped_by: str = ""
    checks: tuple[Check, ...] = ()

    @property
    def failed(self) -> tuple[Check, ...]:
        return tuple(c for c in self.checks if not c.passed)

    def explain(self) -> str:
        if self.approved:
            base = (
                f"APPROVED qty={self.quantity:g} risk=${self.cash_risk:.2f} "
                f"notional=${self.notional:,.0f}"
            )
            return f"{base} (reduced by {self.capped_by})" if self.capped_by else base
        return f"REJECTED — {self.reason}"


def _reject(reason: str, checks: Sequence[Check]) -> RiskDecision:
    return RiskDecision(False, reason=reason, checks=tuple(checks))


def size(
    intent: Intent,
    portfolio: Portfolio,
    cfg: RiskConfig,
    universe: UniverseConfig,
    now: datetime,
    *,
    earnings_blackout: Optional[str] = None,
    data_stale: Optional[str] = None,
    cal: MarketCalendar = CALENDAR,
) -> RiskDecision:
    """Size `intent` or explain why it cannot be taken."""
    now = ensure_utc(now)
    checks: list[Check] = []
    equity = portfolio.equity

    # ── Hard blocks, cheapest first ──────────────────────────────────────────

    br = portfolio.breaker()
    checks.append(Check("circuit_breaker", not br.active, br.reason))
    if br.active:
        return _reject(f"circuit breaker: {br.reason}", checks)

    # A stale feed is the one state where doing nothing strictly dominates. The
    # system's picture of the market has stopped updating while the market has
    # not, and every number below is computed from that frozen picture.
    stale_ok = not (data_stale and cfg.staleness_halts_entries)
    checks.append(Check("data_freshness", stale_ok, data_stale or "fresh"))
    if not stale_ok:
        return _reject(f"stale data: {data_stale}", checks)

    checks.append(Check("earnings_blackout", not earnings_blackout, earnings_blackout or "clear"))
    if earnings_blackout:
        return _reject(f"earnings blackout: {earnings_blackout}", checks)

    already = portfolio.has(intent.symbol)
    checks.append(Check("no_existing_position", not already))
    if already:
        return _reject(f"{intent.symbol}: position already open", checks)

    room = len(portfolio) < cfg.max_open_positions
    checks.append(Check("max_positions", room, f"{len(portfolio)}/{cfg.max_open_positions}"))
    if not room:
        return _reject(f"at position limit ({cfg.max_open_positions})", checks)

    opens_ok = portfolio.opens_today < cfg.max_new_positions_per_day
    checks.append(Check("max_new_per_day", opens_ok,
                        f"{portfolio.opens_today}/{cfg.max_new_positions_per_day}"))
    if not opens_ok:
        return _reject(f"daily new-position cap ({cfg.max_new_positions_per_day})", checks)

    pdt_block, pdt_reason = portfolio.pdt_blocked(now)
    checks.append(Check("pdt_rule", not pdt_block, pdt_reason or "ok"))
    if pdt_block:
        return _reject(pdt_reason, checks)

    sector = universe.sector_of(intent.symbol)
    sec_room = portfolio.sector_count(sector) < cfg.max_positions_per_sector
    checks.append(Check("sector_count", sec_room,
                        f"{sector}: {portfolio.sector_count(sector)}/{cfg.max_positions_per_sector}"))
    if not sec_room:
        return _reject(f"sector '{sector}' at position limit", checks)

    if equity <= 0:
        return _reject("no equity", checks)

    # ── Risk-first sizing ────────────────────────────────────────────────────

    stop_dist = intent.stop_distance
    if stop_dist <= 0:
        return _reject("non-positive stop distance", checks)

    risk_budget = equity * cfg.risk_pct / 100.0

    # Trim the budget so the book's TOTAL open risk stays inside its ceiling.
    # This is what stops eight individually-reasonable positions adding up to
    # an unreasonable day.
    risk_ceiling = equity * cfg.max_total_open_risk_pct / 100.0
    remaining = risk_ceiling - portfolio.total_open_risk()
    checks.append(Check(
        "total_open_risk", remaining > 0,
        f"${portfolio.total_open_risk():.0f}/${risk_ceiling:.0f} used",
    ))
    if remaining <= 0:
        return _reject(
            f"book open risk ${portfolio.total_open_risk():.0f} at ceiling "
            f"${risk_ceiling:.0f}", checks,
        )

    capped_by = ""
    budget = risk_budget
    if remaining < budget:
        budget, capped_by = remaining, "total_open_risk"

    qty = math.floor(budget / stop_dist)
    if qty < 1:
        return _reject(
            f"stop ${stop_dist:.2f} wide — one share risks more than the "
            f"${budget:.2f} budget", checks,
        )

    price = intent.reference_price

    # ── Exposure caps: reduce only ───────────────────────────────────────────

    def cap(limit_notional: float, label: str) -> None:
        nonlocal qty, capped_by
        if limit_notional <= 0:
            qty = 0
            capped_by = label
            return
        allowed = math.floor(limit_notional / price)
        if allowed < qty:
            qty, capped_by = allowed, label

    cap(equity * cfg.max_position_notional_pct / 100.0, "position_notional")
    cap(
        max(0.0, equity * cfg.max_sector_notional_pct / 100.0 - portfolio.sector_notional(sector)),
        "sector_notional",
    )
    cap(max(0.0, equity * cfg.max_gross_exposure - portfolio.gross_notional()), "gross_exposure")

    # Net exposure is directional: a new long is only constrained by how long
    # the book already is. A long added to a net-short book reduces net
    # exposure, so it must not be capped by it.
    net = portfolio.net_notional()
    net_limit = equity * cfg.max_net_exposure
    projected_same_way = net * intent.side.sign
    if projected_same_way > 0:
        cap(max(0.0, net_limit - projected_same_way), "net_exposure")

    if qty < 1:
        return _reject(f"exposure caps leave no room ({capped_by})", checks)

    cash_risk = qty * stop_dist
    notional = qty * price

    checks.append(Check("position_notional", True, f"${notional:,.0f}"))
    checks.append(Check("cash_risk", True, f"${cash_risk:.2f} ({cash_risk/equity*100:.2f}%)"))

    return RiskDecision(
        approved=True,
        quantity=float(qty),
        cash_risk=cash_risk,
        notional=notional,
        reason="risk-sized" if not capped_by else f"reduced by {capped_by}",
        capped_by=capped_by,
        checks=tuple(checks),
    )


def overnight_check(
    portfolio: Portfolio, cfg: RiskConfig, now: datetime, cal: MarketCalendar = CALENDAR
) -> list[str]:
    """Symbols that must be flattened before the close.

    A stop is an order resting against a market. Between 16:00 and 09:30 there
    is no market for it to rest against, so overnight risk is unbounded by the
    stop and bounded only by the size of the gap.
    """
    if cfg.allow_overnight:
        positions = sorted(portfolio.all(), key=lambda p: p.open_risk, reverse=True)
        excess = len(positions) - cfg.max_overnight_positions
        return [p.symbol for p in positions[:excess]] if excess > 0 else []
    return [p.symbol for p in portfolio.all()]


def validate_config(cfg: RiskConfig) -> list[str]:
    """Catch self-contradictory settings at startup rather than at 2am.

    A misconfigured risk envelope does not throw — it silently permits more
    than intended, which is exactly the failure you do not notice until it has
    already cost something.
    """
    problems: list[str] = []
    if cfg.risk_pct <= 0 or cfg.risk_pct > 5:
        problems.append(f"risk_pct {cfg.risk_pct}% outside sane range (0, 5]")
    if cfg.max_total_open_risk_pct < cfg.risk_pct:
        problems.append(
            f"max_total_open_risk_pct {cfg.max_total_open_risk_pct}% below "
            f"risk_pct {cfg.risk_pct}% — no trade could ever be opened"
        )
    if cfg.daily_loss_stop_pct >= cfg.weekly_loss_stop_pct:
        problems.append(
            f"daily stop {cfg.daily_loss_stop_pct}% >= weekly "
            f"{cfg.weekly_loss_stop_pct}% — the weekly breaker can never fire first"
        )
    if cfg.max_net_exposure > cfg.max_gross_exposure:
        problems.append("max_net_exposure exceeds max_gross_exposure (unreachable)")
    if cfg.min_stop_atr_mult > cfg.max_stop_atr_mult:
        problems.append("min_stop_atr_mult exceeds max_stop_atr_mult")
    if cfg.max_positions_per_sector > cfg.max_open_positions:
        problems.append("max_positions_per_sector exceeds max_open_positions (no effect)")
    return problems


__all__ = ["RiskDecision", "Check", "size", "overnight_check", "validate_config"]
