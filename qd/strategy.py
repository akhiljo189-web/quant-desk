"""
qd.strategy — combining four channels into one decision.

The rule that does the work is CONFLUENCE: independent channels must agree
before anything trades.

The reasoning is about error, not about strength. Each channel fails in its own
characteristic way — a busted print inflates volume, an aggregator repeats a
headline, a mislabelled spread leg reads as a directional sweep, a stale
consensus estimate manufactures a surprise. Those failures are largely
uncorrelated. A single channel screaming is therefore much more likely to be
that channel breaking than to be a real opportunity, whereas two channels
breaking in the same direction within the same minutes is rare.

This costs trades. Most genuine opportunities never get a second confirming
channel, and the system will sit through them. That is the intended trade —
lower frequency for a much lower rate of acting on artefacts.

Aggregation happens in two stages, and the order matters:

    within a source   the readings are averaged, not summed
    across sources    the per-source scores are weighted and summed

Summing within a source would let the market channel — which emits four
readings — outvote news, which emits one. That is a counting artefact, not a
finding. Averaging first makes each channel one vote whose strength is its own
internal agreement.

Assess() always returns its full reasoning, including for symbols that do not
trade. The near-misses are the more useful record: they show what the system
was looking at and why it declined, which is what you need when it eventually
does something surprising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Mapping, Optional, Sequence

from qd.clock import CALENDAR, MarketCalendar, Phase
from qd.config import RiskConfig, StrategyConfig
from qd.features.market import MarketSnapshot
from qd.types import (
    Evidence, Intent, Side, Source, clamp, ensure_utc,
)


@dataclass(frozen=True)
class Assessment:
    """The full reasoning behind a decision, including a refusal."""
    symbol: str
    now: datetime
    net_score: float
    conviction: float
    direction: Optional[Side]
    per_source: Mapping[Source, float]
    agreeing_sources: int
    opposing_weight: float
    live_evidence: tuple[Evidence, ...]
    blocked: str = ""              # empty means it passed every gate

    @property
    def would_trade(self) -> bool:
        return self.direction is not None and not self.blocked

    def explain(self) -> str:
        parts = [
            f"{s.value}={v:+.3f}" for s, v in sorted(
                self.per_source.items(), key=lambda kv: -abs(kv[1])
            )
        ]
        head = (
            f"{self.symbol} net={self.net_score:+.3f} conv={self.conviction:.3f} "
            f"dir={self.direction.value if self.direction else '-'} "
            f"agree={self.agreeing_sources} opp={self.opposing_weight:.3f}"
        )
        tail = f" BLOCKED: {self.blocked}" if self.blocked else ""
        return f"{head} [{' '.join(parts)}]{tail}"


def aggregate(
    evidence: Sequence[Evidence], now: datetime, cfg: StrategyConfig
) -> dict[Source, float]:
    """Per-source scores in [-1, 1], time-decayed and confidence-scaled.

    Within a source the readings are combined as a confidence-weighted mean, so
    a channel that emits many weak readings does not outrank one that emits a
    single strong one.
    """
    now = ensure_utc(now)
    buckets: dict[Source, list[tuple[float, float]]] = {}

    for e in evidence:
        if not e.is_live(now):
            continue
        w = e.weight(now)              # decayed score x confidence
        buckets.setdefault(e.source, []).append((w, e.confidence))

    out: dict[Source, float] = {}
    for src, pairs in buckets.items():
        denom = sum(c for _, c in pairs)
        out[src] = clamp(sum(w for w, _ in pairs) / denom) if denom > 0 else 0.0
    return out


def assess(
    symbol: str,
    evidence: Sequence[Evidence],
    snap: MarketSnapshot,
    now: datetime,
    cfg: StrategyConfig,
    cal: MarketCalendar = CALENDAR,
) -> Assessment:
    """Score a symbol and decide whether it clears every gate."""
    now = ensure_utc(now)
    live = tuple(e for e in evidence if e.is_live(now) and e.symbol == symbol.upper())
    per_source = aggregate(live, now, cfg)

    # Weighted combination across channels.
    total_w = sum(cfg.weights.get(s, 0.0) for s in per_source) or 1.0
    net = sum(score * cfg.weights.get(src, 0.0) for src, score in per_source.items()) / total_w
    net = clamp(net)

    direction = Side.BUY if net > 0 else Side.SELL if net < 0 else None

    # How much weight argues each way.
    agreeing = 0
    opposing = 0.0
    if direction is not None:
        want = 1 if direction is Side.BUY else -1
        for src, score in per_source.items():
            if score == 0:
                continue
            contribution = abs(score) * cfg.weights.get(src, 0.0) / total_w
            if (score > 0) == (want > 0):
                # Only count a channel as agreeing if it says something. A
                # score of 0.02 is not a confirmation, it is a shrug.
                if abs(score) >= 0.10:
                    agreeing += 1
            else:
                opposing += contribution

    # Disagreement is penalised harder than agreement is rewarded. When
    # channels conflict the honest reading is that the picture is unclear, and
    # unclear is a reason to stand aside rather than to trade smaller.
    conviction = clamp(abs(net) - opposing * cfg.conflict_penalty, 0.0, 1.0)

    blocked = _gate(
        symbol, direction, conviction, agreeing, opposing, snap, now, cfg, cal
    )

    return Assessment(
        symbol=symbol.upper(),
        now=now,
        net_score=net,
        conviction=conviction,
        direction=direction,
        per_source=per_source,
        agreeing_sources=agreeing,
        opposing_weight=opposing,
        live_evidence=live,
        blocked=blocked,
    )


def _gate(
    symbol: str,
    direction: Optional[Side],
    conviction: float,
    agreeing: int,
    opposing: float,
    snap: MarketSnapshot,
    now: datetime,
    cfg: StrategyConfig,
    cal: MarketCalendar,
) -> str:
    """Every reason not to trade, checked in order. Empty string = clear."""
    if direction is None:
        return "no directional signal"
    if not snap.has_core:
        return "no ATR — cannot place a stop"

    phase = cal.phase(now)
    if phase is Phase.CLOSED:
        return "market closed"
    if phase in (Phase.PREMARKET, Phase.AFTERHOURS) and not cfg.allow_extended_hours:
        return f"{phase.value} trading disabled"

    if phase is Phase.REGULAR:
        since = cal.minutes_since_open(now)
        until = cal.minutes_to_close(now)
        if since is not None and since < cfg.no_entry_first_minutes:
            # The opening auction unwinds into the first prints; spreads are
            # wide and the tape is not yet describing the day.
            return f"within {cfg.no_entry_first_minutes:.0f}min of the open"
        if until is not None and until < cfg.no_entry_last_minutes:
            # A position opened here cannot reach its target before the close,
            # so it becomes an unplanned overnight hold.
            return f"within {cfg.no_entry_last_minutes:.0f}min of the close"

    if agreeing < cfg.min_sources:
        return f"confluence: {agreeing} agreeing source(s), need {cfg.min_sources}"
    if opposing > cfg.veto_on_conflict_above:
        return f"conflicting evidence ({opposing:.2f} opposing weight)"
    if conviction < cfg.min_conviction:
        return f"conviction {conviction:.3f} below {cfg.min_conviction}"
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Intent construction
# ─────────────────────────────────────────────────────────────────────────────

def stop_for(
    side: Side,
    price: float,
    atr: float,
    cfg: StrategyConfig,
    risk: RiskConfig,
    session_high: Optional[float] = None,
    session_low: Optional[float] = None,
) -> float:
    """Place the stop: ATR-based, floored, and pushed beyond session structure.

    Three constraints, applied in order.

      1. ATR distance — the stop must sit outside the range the stock covers in
         ordinary trading, or noise alone removes the position.
      2. A percentage floor — for very quiet names ATR can be small enough that
         the stop lands inside the spread.
      3. Structure — nudge past the session low (long) or high (short). Resting
         a stop exactly at an obvious level is asking to be filled on the wick
         that tests it and then reverses.

    Wider stops mean smaller positions for the same cash risk. That is the
    correct direction of the trade-off: it is better to be right and small than
    stopped out and correct.
    """
    # Order matters. The ATR ceiling is applied FIRST and the absolute floor
    # LAST, so the floor always wins. Reversed, a very quiet name (tiny ATR)
    # has its floored distance clamped straight back down by the ATR ceiling,
    # putting the stop inside the spread — and a too-tight stop buys a LARGER
    # position, so that ordering bug hands out maximum size exactly where the
    # measurement is least reliable.
    dist = atr * cfg_stop_mult(cfg, risk)
    dist = min(dist, atr * risk.max_stop_atr_mult)
    dist = max(dist, price * risk.min_stop_pct)

    if side is Side.BUY:
        stop = price - dist
        if session_low is not None and session_low < price:
            stop = min(stop, session_low - atr * 0.10)
        return max(0.01, stop)

    stop = price + dist
    if session_high is not None and session_high > price:
        stop = max(stop, session_high + atr * 0.10)
    return stop


def cfg_stop_mult(cfg: StrategyConfig, risk: RiskConfig) -> float:
    return max(risk.min_stop_atr_mult, min(risk.default_stop_atr_mult, risk.max_stop_atr_mult))


def build_intent(
    a: Assessment,
    snap: MarketSnapshot,
    cfg: StrategyConfig,
    risk: RiskConfig,
) -> Optional[Intent]:
    """Turn a passing assessment into a proposed trade, or None."""
    if not a.would_trade or a.direction is None or snap.atr is None:
        return None

    price = snap.last
    stop = stop_for(
        a.direction, price, snap.atr, cfg, risk, snap.session_high, snap.session_low
    )
    dist = abs(price - stop)
    if dist <= 0:
        return None

    target = (
        price + dist * cfg.target_r if a.direction is Side.BUY
        else price - dist * cfg.target_r
    )

    intent = Intent(
        symbol=a.symbol,
        side=a.direction,
        conviction=a.conviction,
        reference_price=price,
        stop_price=stop,
        target_price=target,
        created_at=a.now,
        evidence=a.live_evidence,
        notes=a.explain(),
    )

    if intent.reward_risk < cfg.min_reward_risk:
        return None
    return intent


def evaluate_symbol(
    symbol: str,
    evidence: Sequence[Evidence],
    snap: MarketSnapshot,
    now: datetime,
    cfg: StrategyConfig,
    risk: RiskConfig,
    cal: MarketCalendar = CALENDAR,
) -> tuple[Assessment, Optional[Intent]]:
    """Convenience: assess and, if it passes, build the intent."""
    a = assess(symbol, evidence, snap, now, cfg, cal)
    return a, build_intent(a, snap, cfg, risk)


__all__ = [
    "Assessment", "aggregate", "assess", "build_intent", "evaluate_symbol", "stop_for",
]
