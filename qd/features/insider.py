"""
qd.features.insider — insider accumulation, scored from Form 4 transactions.

The Form 4 half of the Big Money Score in
`docs/superpowers/specs/2026-09-05-future-leaders-design.md` §5.3. It consumes
the typed transactions from `qd/providers/forms.py` and answers one question:
how much conviction did this company's own officers and directors show in the
last quarter?

WHAT THIS SCORE IS, AND IS NOT

It is the Form 4 components only — purchase intensity, cluster breadth,
seniority, and a distribution penalty. The design's 13D/G (15%) and 13F (10%)
components do not exist yet, so `components_absent` names them on every result.
That field is not decoration: a partial score silently compared against the
design's weighting reads as a weak signal when it is an incomplete one.

THE FOUR WAYS AN AGGREGATE INFLATES ITSELF, all of which produce a higher
number from the same underlying conviction:

  ONE PERSON, MANY FILINGS   An insider building a position over a quarter
                             files repeatedly. That is one decision revisited,
                             not a crowd. Cluster counts distinct people.

  MANY PEOPLE, ONE FILING    A joint filing names a spouse or a family trust
                             alongside the insider. Two names, one decision.
                             Cluster counts distinct people AND distinct
                             filings, and takes the smaller.

  AMENDMENTS                 A 4/A restates a filing already counted. Keeping
                             both reports one purchase as two, and amendments
                             concentrate on large complicated filers, so the
                             inflation is not random across the cross-section.

  DOLLARS AS CONVICTION      A wealthy CEO's $2M buy is a smaller commitment
                             than a junior officer's $200k. Intensity is
                             measured against what the insider ALREADY HELD,
                             which makes it a statement about their own
                             portfolio rather than about their salary history.

WHY SILENCE SCORES ZERO AND NOT NEGATIVE

Most companies in a cross-section have no insider activity in a given quarter.
If silence scored negative, the ranking would be driven by filing frequency —
which tracks company size, board size and compensation structure. Absence of
evidence is scored as absence of evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from qd.providers.forms import InsiderTransaction, Seniority
from qd.types import ensure_utc

# One quarter. Long enough for a cluster to form across a board that meets
# monthly; short enough that the score reflects the current view rather than
# last year's. The design registers 90 days for cluster breadth, so the whole
# score uses one window rather than several — a second window would be a
# second free parameter with no evidence behind it.
WINDOW_DAYS = 90

# Component weights, from the design's §5.3, renormalised across the Form 4
# components alone so the score spans 0-100 on its own terms. The design's
# absolute weights were 35/20/10 of a 100-point Big Money Score; those ratios
# are preserved exactly. `components_absent` records what the remaining 25
# points would have covered.
W_INTENSITY = 35.0 / 65.0
W_CLUSTER = 20.0 / 65.0
W_SENIORITY = 10.0 / 65.0

# The distribution penalty, applied as a multiplier rather than a subtraction
# so it cannot drive an accumulation score below zero. -10 of 65 at full
# strength, matching the design's weight.
MAX_SELL_PENALTY = 10.0 / 65.0

# Cluster breadth saturates: four independent insiders buying is a strong
# signal, and the eighth adds little. Boards have finite size, so an unbounded
# count would rank by board size.
CLUSTER_SATURATION = 4.0

# A purchase adding this much to an insider's existing stake scores full
# intensity. A 50% increase in a personal holding is a large commitment; the
# cap keeps a first-ever purchase (an infinite ratio) from dominating.
INTENSITY_SATURATION = 0.5

ABSENT_COMPONENTS = (
    "13D/G accumulation (15% of the design's Big Money Score) — parser not built",
    "13F institutional breadth (10%) — parser not built",
)


@dataclass(frozen=True, slots=True)
class InsiderScore:
    """An accumulation reading for one symbol at one instant.

    Every component is reported alongside the blend, because the blend is a
    hypothesis about how to weight them and the components are measurements.
    When this is evaluated by decile, the components are what get tested first
    — the design forbids testing the blend before its parts.
    """
    symbol: str
    as_of: datetime
    score: float                       # 0-100, the Form 4 components only
    intensity: float                   # 0-1, purchase size vs existing stake
    cluster_size: int                  # distinct insiders, joint filings merged
    top_seniority: Seniority
    purchase_count: int
    purchase_value: float
    sale_value: float
    components_absent: tuple[str, ...] = field(default=ABSENT_COMPONENTS)

    @property
    def is_divergence_candidate(self) -> bool:
        """Insiders buying with no offsetting distribution.

        The price half of the design's Smart Money Divergence detector lives in
        the caller — this side only reports that the insider leg is clean.
        """
        return self.purchase_count > 0 and self.sale_value <= 0.0


def score_insiders(
    transactions: Iterable[InsiderTransaction],
    *,
    as_of: datetime,
    symbol: Optional[str] = None,
    window_days: int = WINDOW_DAYS,
) -> InsiderScore:
    """Score insider accumulation as of `as_of`, from whatever was public then.

    `as_of` is a hard cut on `known_at`, not on the transaction date. A trade
    made three days ago whose Form 4 lands tomorrow is invisible today, and a
    trade made four months ago whose filing landed last week is visible now —
    the window is about when this system could have acted, which is the only
    thing a backtest is allowed to know.
    """
    now = ensure_utc(as_of)
    cutoff = now - timedelta(days=window_days)

    rows = _visible(transactions, now=now, cutoff=cutoff, symbol=symbol)
    purchases = [t for t in rows if t.is_open_market_purchase]
    sales = [t for t in rows if t.is_open_market_sale]

    purchase_value = sum(t.value or 0.0 for t in purchases)
    sale_value = sum(t.value or 0.0 for t in sales)

    resolved_symbol = symbol or (rows[0].symbol if rows else "")

    if not purchases:
        return InsiderScore(
            symbol=resolved_symbol, as_of=now, score=0.0, intensity=0.0,
            cluster_size=0, top_seniority=Seniority.OTHER, purchase_count=0,
            purchase_value=0.0, sale_value=sale_value,
        )

    intensity = _intensity(purchases)
    cluster = _cluster_size(purchases)
    seniority = max(t.seniority for t in purchases)

    raw = (
        W_INTENSITY * intensity
        + W_CLUSTER * min(cluster / CLUSTER_SATURATION, 1.0)
        + W_SENIORITY * (seniority / Seniority.CEO)
    )
    score = 100.0 * max(0.0, raw * (1.0 - _sell_penalty(purchase_value, sale_value)))

    return InsiderScore(
        symbol=resolved_symbol,
        as_of=now,
        score=min(100.0, score),
        intensity=intensity,
        cluster_size=cluster,
        top_seniority=seniority,
        purchase_count=len(purchases),
        purchase_value=purchase_value,
        sale_value=sale_value,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Components
# ─────────────────────────────────────────────────────────────────────────────

def _visible(
    transactions: Iterable[InsiderTransaction],
    *,
    now: datetime,
    cutoff: datetime,
    symbol: Optional[str],
) -> list[InsiderTransaction]:
    """Rows this system could have read, in the window, deduplicated.

    Amendments are dropped rather than merged. A 4/A corrects a filing that was
    already counted, and the correction was not available when the original
    was filed — the same restatement rule `providers/xbrl.py` applies to
    fundamentals, for the same reason.
    """
    out: list[InsiderTransaction] = []
    for t in transactions:
        if symbol and t.symbol != symbol:
            continue
        if t.is_amendment:
            continue
        if not (cutoff <= t.known_at <= now):
            continue
        out.append(t)
    return out


def _intensity(purchases: Sequence[InsiderTransaction]) -> float:
    """Mean purchase size relative to the buyer's existing stake, 0-1.

    Relative rather than absolute on purpose: a dollar-weighted measure ranks
    by executive wealth, which is a function of tenure and company size, and
    tracks the same things a size factor already tracks.

    A first-ever purchase — no prior holding — is the strongest form of this
    signal and also the row that breaks the ratio, so it takes the cap rather
    than an infinity or a zero.
    """
    ratios: list[float] = []
    for t in purchases:
        if t.shares_owned_after is None:
            # No post-transaction holding disclosed. Score it at the midpoint
            # rather than dropping the row: dropping would silently favour
            # filers whose agents fill in more fields.
            ratios.append(0.5)
            continue
        prior = t.shares_owned_after - t.shares
        if prior <= 0:
            ratios.append(1.0)          # bought their entire position today
            continue
        ratios.append(min(t.shares / prior / INTENSITY_SATURATION, 1.0))
    return sum(ratios) / len(ratios) if ratios else 0.0


def _cluster_size(purchases: Sequence[InsiderTransaction]) -> int:
    """Independent insiders buying, joint filings collapsed.

    Takes the smaller of (distinct people, distinct filings). One person filing
    three times is one; three people named on one filing is one; three people
    filing separately is three. Ten-percent owners are excluded entirely — a
    sponsor or fund trades for portfolio reasons and is not an insider forming
    a view, which is the distinction the design's falsification test 3 turns on.
    """
    insiders = [t for t in purchases if t.seniority > Seniority.TEN_PERCENT]
    if not insiders:
        return 0
    return min(
        len({t.owner_cik for t in insiders}),
        len({t.accession for t in insiders}),
    )


def _sell_penalty(purchase_value: float, sale_value: float) -> float:
    """How much distribution offsets the accumulation, 0 to MAX_SELL_PENALTY.

    A multiplier rather than a subtraction, so heavy selling can neutralise a
    buy signal but never produce a negative accumulation score. Insider selling
    is weak evidence in isolation — insiders sell to buy houses, diversify and
    pay tax bills — and the design weights it accordingly.
    """
    if sale_value <= 0.0:
        return 0.0
    total = purchase_value + sale_value
    if total <= 0.0:
        return 0.0
    return MAX_SELL_PENALTY * (sale_value / total)


__all__ = ["InsiderScore", "score_insiders", "WINDOW_DAYS"]
