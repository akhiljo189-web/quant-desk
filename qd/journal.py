"""
qd.journal — append-only record of every decision.

Not logging. Logging is for watching the system run; the journal is for
answering, weeks later, "why did it buy that?" — with the actual numbers that
were in front of it at the time, not a reconstruction from memory.

It records REFUSALS as well as trades, and the refusals are the more useful
half. A system that trades three times a day makes hundreds of decisions not
to, and if those are invisible then the only evidence about its behaviour is the
handful of cases where it acted. That is a biased sample of its own reasoning:
the near-misses show whether the thresholds are doing anything, or whether the
system is one bad tick from a trade it should never take.

Append-only JSONL, one object per line, flushed on write. If the process dies
mid-decision the record of everything before it survives — which is exactly
when you most want to know what it was thinking.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta
from enum import Enum
from typing import Any, Iterable, Iterator, Optional

from qd.types import Evidence, Intent, Position, utcnow

logger = logging.getLogger(__name__)


def _encode(obj: Any) -> Any:
    """JSON encoder for the record types."""
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    return str(obj)


class Journal:
    """Append-only decision log."""

    def __init__(self, path: str, echo: bool = False) -> None:
        self.path = path
        self.echo = echo
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def write(self, kind: str, **fields: Any) -> None:
        record = {"ts": utcnow().isoformat(), "kind": kind, **fields}
        line = json.dumps(record, default=_encode, separators=(",", ":"))
        with self._lock:
            with open(self.path, "a") as fh:
                fh.write(line + "\n")
                fh.flush()
        if self.echo:
            logger.info("journal[%s] %s", kind, fields.get("symbol", ""))

    # ── typed helpers ────────────────────────────────────────────────────────

    def assessment(self, a, taken: bool, blocked: str = "") -> None:
        """Every symbol looked at, traded or not."""
        self.write(
            "assessment",
            symbol=a.symbol,
            net_score=round(a.net_score, 4),
            conviction=round(a.conviction, 4),
            direction=a.direction.value if a.direction else None,
            agreeing=a.agreeing_sources,
            opposing=round(a.opposing_weight, 4),
            per_source={s.value: round(v, 4) for s, v in a.per_source.items()},
            evidence=[
                {
                    "source": e.source.value, "kind": e.kind,
                    "score": round(e.score, 4), "confidence": round(e.confidence, 4),
                    "observed_at": e.observed_at.isoformat(),
                    "detail": dict(e.detail),
                }
                for e in a.live_evidence
            ],
            taken=taken,
            blocked=blocked or a.blocked,
        )

    def risk_decision(self, intent: Intent, decision) -> None:
        self.write(
            "risk",
            symbol=intent.symbol,
            side=intent.side.value,
            approved=decision.approved,
            quantity=decision.quantity,
            cash_risk=round(decision.cash_risk, 2),
            notional=round(decision.notional, 2),
            reason=decision.reason,
            capped_by=decision.capped_by,
            checks=[
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in decision.checks
            ],
        )

    def order(self, intent: Intent, broker_order, quantity: float) -> None:
        self.write(
            "order",
            symbol=intent.symbol,
            side=intent.side.value,
            quantity=quantity,
            reference_price=intent.reference_price,
            stop_price=intent.stop_price,
            target_price=intent.target_price,
            reward_risk=round(intent.reward_risk, 3),
            conviction=round(intent.conviction, 4),
            client_order_id=intent.idempotency_key(),
            broker_order_id=getattr(broker_order, "id", None),
            sources=[s.value for s in intent.sources()],
        )

    def fill(self, symbol: str, side: str, quantity: float, price: float, kind: str) -> None:
        self.write("fill", symbol=symbol, side=side, quantity=quantity,
                   price=price, fill_kind=kind)

    def exit(self, trade) -> None:
        self.write(
            "exit",
            symbol=trade.symbol,
            side=trade.side.value,
            quantity=trade.quantity,
            entry_price=trade.entry_price,
            exit_price=trade.exit_price,
            pnl=round(trade.pnl, 2),
            r_multiple=round(trade.r_multiple, 3),
            reason=trade.reason,
            hold_seconds=trade.hold_time.total_seconds(),
        )

    def event(self, message: str, **fields: Any) -> None:
        self.write("event", message=message, **fields)

    def error(self, message: str, **fields: Any) -> None:
        self.write("error", message=message, **fields)

    # ── reading ──────────────────────────────────────────────────────────────

    def read(self, kinds: Optional[Iterable[str]] = None) -> Iterator[dict]:
        if not os.path.exists(self.path):
            return
        want = set(kinds) if kinds else None
        with open(self.path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue        # a torn final line from a hard kill
                if want is None or rec.get("kind") in want:
                    yield rec

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for rec in self.read():
            counts[rec.get("kind", "?")] = counts.get(rec.get("kind", "?"), 0) + 1
        return counts

    def blocked_reasons(self) -> dict[str, int]:
        """Why trades did NOT happen, most common first.

        The most valuable query in the file. If one reason dominates, that gate
        is the system's real strategy — everything else is decoration.
        """
        counts: dict[str, int] = {}
        for rec in self.read(["assessment"]):
            if rec.get("taken"):
                continue
            reason = rec.get("blocked") or "unknown"
            head = reason.split("(")[0].split(":")[0].strip()
            counts[head] = counts.get(head, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


__all__ = ["Journal"]
