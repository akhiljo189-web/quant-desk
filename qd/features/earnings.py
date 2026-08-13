"""
qd.features.earnings — report scheduling and post-earnings drift, channel 3 of 4.

This channel does two jobs, and the first is more valuable than the second.

1. BLACKOUT. Keep the system out of positions across a scheduled print. An
   earnings release is a scheduled discontinuity: the stock does not travel to
   its new price, it appears there when the auction reopens. A stop resting at
   -1R does not fill at -1R across a gap; it fills wherever the first print
   lands, which is routinely -4R and occasionally worse. Every risk number
   elsewhere in this system assumes the stop approximately holds, and that
   assumption is false exactly here.

2. PEAD. Post-earnings announcement drift — prices continue in the direction of
   an earnings surprise for weeks — is among the most replicated anomalies in
   the literature (Ball & Brown 1968, and a long line since). It has also decayed
   markedly as it became famous and as more of the reaction moved into the first
   minutes. Treated here as one evidence channel among four, never as a reason
   to trade by itself.

The subtle part is what "surprise" means. Consensus EPS is one number and a
quarter is a whole story: guidance, margins, segment mix, buyback pace. A stock
that beats EPS and falls has told you the beat was not the point. So when the
reported surprise and the market's own reaction disagree, this module follows
the reaction and cuts confidence — the tape has seen the full release and the
consensus number has not.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Optional, Sequence

from qd.clock import CALENDAR, ET, MarketCalendar
from qd.config import EarningsConfig
from qd.types import UTC, EarningsEvent, Evidence, Source, ensure_utc, squash, clamp


def expected_release_time(ev: EarningsEvent, cal: MarketCalendar = CALENDAR) -> datetime:
    """When the numbers are expected to hit, from the bmo/amc marker.

    Before-open reports land around 07:00 ET, after-close around 16:15 ET.
    Approximate by design — the blackout window is measured in hours and does
    not need the release timed to the minute.

    `report_date` is a CALENDAR DATE stored at 00:00 UTC, so its date component
    is read directly. Converting it to Eastern first would roll it back a day
    (00:00 UTC is 19:00 or 20:00 the previous evening in New York), which
    computes the blackout for the wrong session and lets the system hold a
    position straight through the print it was supposed to avoid.
    """
    d = ev.report_date.date()
    if ev.session == "bmo":
        return datetime.combine(d, time(7, 0), tzinfo=ET).astimezone(UTC)
    if ev.session == "amc":
        return datetime.combine(d, time(16, 15), tzinfo=ET).astimezone(UTC)
    # Unknown session: assume the worst case, an open-hours release.
    return datetime.combine(d, time(12, 0), tzinfo=ET).astimezone(UTC)


@dataclass(frozen=True)
class BlackoutState:
    active: bool
    reason: str
    event: Optional[EarningsEvent] = None
    hours_until: Optional[float] = None


def blackout(
    symbol: str,
    events: Sequence[EarningsEvent],
    now: datetime,
    cfg: EarningsConfig,
    cal: MarketCalendar = CALENDAR,
) -> BlackoutState:
    """Whether `symbol` is inside an earnings blackout at `now`.

    Only events whose SCHEDULE was already known are considered — learning on
    Tuesday that a company reported on Monday cannot retroactively have kept us
    out of the trade.
    """
    now = ensure_utc(now)
    sym = symbol.upper()

    for ev in events:
        if ev.symbol.upper() != sym:
            continue
        if ev.known_at > now:
            continue                       # schedule not yet published

        release = expected_release_time(ev, cal)
        delta = release - now

        if timedelta(0) <= delta <= cfg.blackout_before:
            return BlackoutState(
                True,
                f"earnings in {delta.total_seconds()/3600:.1f}h ({ev.session})",
                ev, delta.total_seconds() / 3600.0,
            )

        # Immediately after the release: the print is out but the spread is
        # enormous and the first prints are unreliable. Wait for the auction.
        if -cfg.blackout_after <= delta < timedelta(0):
            return BlackoutState(
                True,
                f"post-release settling ({-delta.total_seconds()/60:.0f}m since print)",
                ev, delta.total_seconds() / 3600.0,
            )

    return BlackoutState(False, "clear")


def next_event(
    symbol: str, events: Sequence[EarningsEvent], now: datetime
) -> Optional[EarningsEvent]:
    """Next known-scheduled report for a symbol, or None."""
    now = ensure_utc(now)
    upcoming = [
        e for e in events
        if e.symbol.upper() == symbol.upper()
        and e.known_at <= now
        and expected_release_time(e) >= now
    ]
    return min(upcoming, key=expected_release_time) if upcoming else None


def evaluate(
    symbol: str,
    events: Sequence[EarningsEvent],
    now: datetime,
    cfg: EarningsConfig,
    reaction_pct: Optional[float] = None,
) -> list[Evidence]:
    """Score post-earnings drift.

    `reaction_pct` is the underlying's move since the release — the market's
    verdict on the full report. Supply it when known; without it the surprise
    numbers are scored alone at reduced confidence, because consensus EPS on
    its own is a poor summary of what a quarter actually said.
    """
    now = ensure_utc(now)
    out: list[Evidence] = []
    sym = symbol.upper()

    for ev in events:
        if ev.symbol.upper() != sym:
            continue

        # The actuals gate, distinct from the schedule gate. This is where the
        # fundamental-data leak lives: the schedule is known days ahead, the
        # numbers are not, and a record carrying both invites treating them as
        # equally available.
        if not ev.has_actuals_at(now):
            continue

        released = ev.actuals_known_at()
        assert released is not None
        age = now - released
        if age > cfg.pead_window:
            continue

        eps_s = ev.eps_surprise_pct()
        rev_s = ev.revenue_surprise_pct()
        if eps_s is None and rev_s is None and reaction_pct is None:
            continue

        # Blend the two reported surprises; EPS carries more weight because
        # revenue beats with margin compression are common and not bullish.
        parts: list[tuple[float, float]] = []
        if eps_s is not None:
            parts.append((squash(eps_s, cfg.surprise_significant), 0.65))
        if rev_s is not None:
            parts.append((squash(rev_s, cfg.revenue_significant), 0.35))
        reported = (
            sum(s * w for s, w in parts) / sum(w for _, w in parts) if parts else 0.0
        )

        conf = cfg.min_confidence + 0.35
        score = reported
        agreement: Optional[str] = None

        if reaction_pct is not None:
            reaction = squash(reaction_pct, 3.0)   # a 3% move is a decisive verdict
            if not parts:
                score, agreement, conf = reaction, "reaction_only", conf * 0.8
            elif reported == 0.0:
                score, agreement = reaction, "reaction_only"
            elif (reported > 0) == (reaction > 0):
                # Consensus and tape agree — the strongest version of this signal.
                score = 0.4 * reported + 0.6 * reaction
                agreement, conf = "aligned", min(1.0, conf + 0.25)
            else:
                # They disagree. Follow the tape, discounted: the market read
                # the whole release and the EPS line did not.
                #
                # The multiplier is set so this branch stays ABOVE
                # min_confidence. Discounting harder would drop every
                # conflicting reading below the floor, making this code
                # unreachable — worse than not having it, because it would look
                # like the conflict case was handled when it never fired.
                score, agreement, conf = reaction * 0.6, "conflict", conf * 0.60

        # Drift fades across the window; weight the tail down.
        decay = max(0.0, 1.0 - age / cfg.pead_window)
        conf = clamp(conf * (0.5 + 0.5 * decay), 0.0, 1.0)
        if conf < cfg.min_confidence or abs(score) < 0.05:
            continue

        out.append(Evidence(
            source=Source.EARNINGS,
            kind="pead",
            symbol=sym,
            score=score,
            confidence=conf,
            observed_at=released,
            ttl=cfg.ttl,
            detail={
                "fiscal_period": ev.fiscal_period,
                "eps_actual": ev.eps_actual,
                "eps_estimate": ev.eps_estimate,
                "eps_surprise_pct": round(eps_s, 4) if eps_s is not None else None,
                "revenue_surprise_pct": round(rev_s, 4) if rev_s is not None else None,
                "reaction_pct": round(reaction_pct, 3) if reaction_pct is not None else None,
                "agreement": agreement,
                "hours_since_release": round(age.total_seconds() / 3600.0, 2),
            },
        ))

    return out


__all__ = ["blackout", "BlackoutState", "evaluate", "next_event", "expected_release_time"]
