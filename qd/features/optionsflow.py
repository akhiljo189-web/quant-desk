"""
qd.features.optionsflow — reading the options tape, channel 4 of 4.

The premise is that large, urgent, opening options positions sometimes reveal a
directional view held by someone with more conviction (or better information)
than the market. The premise is partly true and mostly buried in noise, because
the overwhelming majority of the options tape is one of:

  * market makers hedging inventory — mechanical, carries no view;
  * multi-leg structures printed leg by leg, where reading one leg alone
    inverts or invents the direction;
  * closing trades, which are someone exiting a view, not expressing one;
  * retail lottery tickets in weekly far-OTM strikes;
  * index and ETF hedging that says nothing about the single names inside it.

So most of this module is subtraction. It discards more of the tape than it
keeps, and the parts that survive are scored relative to the symbol's own
history rather than in absolute dollars.

Four things make a print interesting, and it needs several of them at once:

  AGGRESSOR   Did the trade lift the offer or hit the bid? Unsigned option
              volume is close to useless directionally — every contract has a
              buyer and a seller, and only the NBBO says which one was in a
              hurry. Trades inside the spread stay unclassified rather than
              being guessed at.

  URGENCY     A sweep — one order shredded across multiple exchanges within
              milliseconds — means the buyer accepted worse fills to finish
              now. Patient money works a single venue and waits.

  NEW RISK    Size above prior open interest means a position is being opened.
              Without that check, someone closing a losing bet reads exactly
              like someone opening a confident one.

  STRUCTURE   Is this a naked directional bet or one leg of a spread? A bought
              call inside a straddle is not bullish at all, and counting it as
              bullish is the single most common error in retail flow reading.

Everything is premium-weighted rather than contract-weighted: 10,000 contracts
of a $0.03 weekly is $30k of conviction, and 200 contracts of a $12 LEAP is
$240k. Contract counts make the first look eight times more significant.
"""

from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from qd.config import OptionsFlowConfig
from qd.types import (
    Aggressor, Evidence, OptionTrade, Right, Source, clamp, ensure_utc, squash,
)

# ─────────────────────────────────────────────────────────────────────────────
# Enriched trade
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FlowTrade:
    """An option print with everything we inferred about it."""
    trade: OptionTrade
    aggressor: Aggressor
    structure: str            # outright | vertical | straddle | risk_reversal | calendar
    is_sweep: bool
    is_block: bool
    opening: Optional[bool]
    direction: float          # [-1, 1] bullish/bearish lean of this print
    weight: float             # conviction multiplier applied to premium
    reject: str = ""          # non-empty means excluded, with the reason

    @property
    def signed_premium(self) -> float:
        """Premium signed by direction and scaled by conviction."""
        if self.reject:
            return 0.0
        return self.trade.premium * self.direction * self.weight


# Direction of a naked position, by right and who initiated.
#
# Bought options are weighted above sold ones. Buying premium is a defined-risk
# bet that needs a move of a given size within a given time to pay — it is a
# genuine directional statement. Selling premium is frequently an income or
# hedging trade against stock already held: a covered call is not a bearish
# view, and a cash-secured put is not really a bullish one.
_DIRECTION: dict[tuple[Right, Aggressor], float] = {
    (Right.CALL, Aggressor.BUY): 1.00,
    (Right.CALL, Aggressor.SELL): -0.55,
    (Right.PUT, Aggressor.BUY): -1.00,
    (Right.PUT, Aggressor.SELL): 0.55,
}


def _moneyness_weight(trade: OptionTrade, cfg: OptionsFlowConfig) -> float:
    """Down-weight strikes far from spot.

    A near-the-money option moves close to one-for-one with the stock and costs
    real money; a 30%-OTM weekly is a lottery ticket whose premium overstates
    the position it represents. This is a coarse stand-in for delta — computing
    a real delta needs implied vol we do not have on the tape.
    """
    spot = trade.underlying_price
    if not spot or spot <= 0:
        return 0.7                     # no spot: assume mid-quality, do not reward
    m = abs(trade.contract.moneyness(spot))
    if m > cfg.max_abs_moneyness:
        return 0.0
    return math.exp(-m / 0.06)         # ~0.44 at 5% OTM, ~0.08 at 15%


def _dte_weight(trade: OptionTrade, now: datetime, cfg: OptionsFlowConfig) -> float:
    """Prefer the horizon where informed directional bets actually live.

    Very short-dated flow is dominated by gamma scalping and pinning; very
    long-dated flow is strategic positioning that says little about the next
    few hours. The middle is where a view on an imminent move gets expressed.
    """
    dte = trade.contract.dte(now)
    if dte < cfg.min_dte or dte > cfg.max_dte:
        return 0.0
    if dte <= 2:
        return 0.55
    if dte <= 21:
        return 1.0
    if dte <= 45:
        return 0.85
    return 0.6


# ─────────────────────────────────────────────────────────────────────────────
# Structure detection
# ─────────────────────────────────────────────────────────────────────────────

def detect_structures(
    trades: Sequence[OptionTrade], cfg: OptionsFlowConfig
) -> dict[int, str]:
    """Label prints that are legs of a multi-leg structure.

    Returns {index in `trades` -> structure label}. Unlabelled prints are
    treated as outright.

    Pairing rule: two prints on the same underlying, within a few hundred
    milliseconds, with sizes matching to within a tolerance. That is a
    deliberately loose test — it will occasionally pair two unrelated prints —
    but the asymmetry favours it. Missing a real spread means scoring a
    non-directional structure as a strong directional bet; falsely pairing two
    unrelated trades only costs some confidence on both.
    """
    labels: dict[int, str] = {}
    order = sorted(range(len(trades)), key=lambda i: trades[i].ts)

    for pos, i in enumerate(order):
        if i in labels:
            continue
        a = trades[i]
        for j in order[pos + 1:]:
            if j in labels:
                continue
            b = trades[j]
            if b.ts - a.ts > cfg.spread_window:
                break
            if b.contract.underlying != a.contract.underlying:
                continue
            # Sizes must roughly match — legs of one structure trade together.
            big = max(a.size, b.size)
            if big <= 0 or abs(a.size - b.size) / big > cfg.spread_size_tolerance:
                continue

            same_right = a.contract.right is b.contract.right
            same_strike = abs(a.contract.strike - b.contract.strike) < 1e-9
            same_expiry = a.contract.expiry == b.contract.expiry
            agg_a, agg_b = a.aggressor(), b.aggressor()
            opposed = (
                agg_a in (Aggressor.BUY, Aggressor.SELL)
                and agg_b in (Aggressor.BUY, Aggressor.SELL)
                and agg_a is not agg_b
            )

            if same_right and same_expiry and not same_strike and opposed:
                label = "vertical"          # directional, but capped payoff
            elif same_right and same_strike and not same_expiry:
                label = "calendar"          # a bet on time, not direction
            elif not same_right and agg_a is agg_b:
                label = "straddle"          # long or short volatility, not direction
            elif not same_right and opposed:
                label = "risk_reversal"     # strongly directional
            else:
                continue

            labels[i] = labels[j] = label
            break

    return labels


_STRUCTURE_WEIGHT: dict[str, float] = {
    "outright": 1.00,
    "vertical": 0.65,       # real direction, capped upside, so less conviction implied
    "risk_reversal": 1.10,  # financing a call by selling a put is a firm view
    "calendar": 0.0,        # says nothing about direction
    "straddle": 0.0,        # explicitly non-directional; scoring it is the classic error
}


def detect_sweeps(
    trades: Sequence[OptionTrade], cfg: OptionsFlowConfig
) -> set[int]:
    """Indices belonging to an intermarket sweep.

    Grouped by contract and aggressor side: a cluster of prints on the same
    contract, same side, across several exchanges inside the sweep window, with
    meaningful total premium. Requiring multiple *exchanges* is what separates
    a sweep from a single large fill printed in pieces on one venue.
    """
    out: set[int] = set()
    groups: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, t in enumerate(trades):
        agg = t.aggressor()
        if agg in (Aggressor.MID, Aggressor.UNKNOWN):
            continue
        groups[(t.contract.occ_symbol or _key(t), agg.value)].append(i)

    for idxs in groups.values():
        idxs.sort(key=lambda i: trades[i].ts)
        start = 0
        for end in range(len(idxs)):
            while trades[idxs[end]].ts - trades[idxs[start]].ts > cfg.sweep_window:
                start += 1
            window = idxs[start: end + 1]
            if len(window) < cfg.sweep_min_legs:
                continue
            exchanges = {trades[i].exchange for i in window if trades[i].exchange}
            if len(exchanges) < cfg.sweep_min_exchanges:
                continue
            if sum(trades[i].premium for i in window) < cfg.sweep_min_premium:
                continue
            out.update(window)

    return out


def _key(t: OptionTrade) -> str:
    c = t.contract
    return f"{c.underlying}|{c.expiry:%Y%m%d}|{c.strike}|{c.right.value}"


# ─────────────────────────────────────────────────────────────────────────────
# Enrichment
# ─────────────────────────────────────────────────────────────────────────────

def enrich(
    trades: Sequence[OptionTrade], now: datetime, cfg: OptionsFlowConfig
) -> list[FlowTrade]:
    """Classify every print and compute its signed, weighted premium."""
    now = ensure_utc(now)
    structures = detect_structures(trades, cfg)
    sweeps = detect_sweeps(trades, cfg)
    out: list[FlowTrade] = []

    for i, t in enumerate(trades):
        agg = t.aggressor()
        structure = structures.get(i, "outright")
        is_sweep = i in sweeps
        is_block = t.size >= cfg.block_size or t.premium >= cfg.block_premium
        opening = t.opens_position_likely()

        reject = ""
        if t.premium < cfg.min_trade_premium:
            reject = "below premium floor"
        elif agg in (Aggressor.MID, Aggressor.UNKNOWN):
            # Not a judgement call. Without a signable trade we do not know who
            # was in a hurry, and inventing a direction here is precisely how
            # noise becomes "institutional accumulation".
            reject = f"unsignable ({agg.value})"
        elif cfg.require_opening_likely and opening is False:
            reject = "likely closing (size <= open interest)"

        # A very wide quote makes the aggressor read meaningless — "at the ask"
        # on a 0.05/0.60 market says nothing about urgency.
        if not reject and t.nbbo_bid is not None and t.nbbo_ask is not None:
            mid = t.nbbo_mid or 0.0
            if mid > 0 and (t.nbbo_ask - t.nbbo_bid) / mid > 1.0:
                reject = "quote too wide to sign"

        direction = _DIRECTION.get((t.contract.right, agg), 0.0)

        weight = 1.0
        weight *= _STRUCTURE_WEIGHT.get(structure, 1.0)
        weight *= _moneyness_weight(t, cfg)
        weight *= _dte_weight(t, now, cfg)
        if is_sweep:
            weight *= 1.35
        if is_block:
            weight *= 1.15
        if opening is True:
            weight *= 1.20
        elif opening is False:
            weight *= 0.50      # probably an exit, not a new view

        if not reject and weight <= 0.0:
            reject = "filtered by structure/moneyness/dte"

        out.append(FlowTrade(
            trade=t, aggressor=agg, structure=structure, is_sweep=is_sweep,
            is_block=is_block, opening=opening, direction=direction,
            weight=weight, reject=reject,
        ))

    return out


# ─────────────────────────────────────────────────────────────────────────────
# Baseline
# ─────────────────────────────────────────────────────────────────────────────

class FlowBaseline:
    """Per-symbol history of daily options premium.

    Absolute premium is uninterpretable. $2m of call premium is an unremarkable
    hour in NVDA and a once-a-year event in a mid-cap, so scoring is against
    each symbol's own distribution. Without this, every signal the system fires
    is simply "this is a large-cap".
    """

    __slots__ = ("_hist", "_max_days")

    def __init__(self, max_days: int = 60) -> None:
        self._hist: dict[str, list[float]] = defaultdict(list)
        self._max_days = max_days

    def record(self, symbol: str, total_premium: float) -> None:
        h = self._hist[symbol.upper()]
        h.append(total_premium)
        if len(h) > self._max_days:
            del h[: len(h) - self._max_days]

    def stats(self, symbol: str) -> Optional[tuple[float, float, int]]:
        h = self._hist.get(symbol.upper(), [])
        if len(h) < 5:
            return None
        mean = statistics.fmean(h)
        sd = statistics.pstdev(h) if len(h) > 1 else 0.0
        return mean, sd, len(h)

    def zscore(self, symbol: str, value: float) -> Optional[float]:
        st = self.stats(symbol)
        if st is None:
            return None
        mean, sd, _ = st
        if sd <= 0:
            return None
        return (value - mean) / sd

    def load(self, data: dict[str, Sequence[float]]) -> None:
        for k, v in data.items():
            self._hist[k.upper()] = list(v)[-self._max_days:]

    def dump(self) -> dict[str, list[float]]:
        return {k: list(v) for k, v in self._hist.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FlowSummary:
    symbol: str
    window_start: datetime
    window_end: datetime
    bullish_premium: float
    bearish_premium: float
    total_premium: float          # conviction-WEIGHTED magnitude, drives imbalance
    signable_premium: float       # raw dollars of the prints we could classify
    raw_premium: float            # everything on the tape, before filters
    imbalance: float              # [-1, 1]
    trade_count: int
    kept_count: int
    sweep_count: int
    block_count: int
    opening_fraction: Optional[float]
    top: tuple[dict, ...] = ()

    @property
    def signable_fraction(self) -> float:
        """Share of tape premium we could actually classify, in raw dollars.

        Must be computed from unweighted premium: the conviction multipliers
        (sweep, opening) exceed 1.0, so dividing weighted by raw would report
        fractions above 100% and quietly inflate confidence.
        """
        if self.raw_premium <= 0:
            return 0.0
        return min(1.0, self.signable_premium / self.raw_premium)


def summarise(
    symbol: str,
    trades: Sequence[OptionTrade],
    now: datetime,
    cfg: OptionsFlowConfig,
) -> FlowSummary:
    """Aggregate a window of the tape for one underlying."""
    now = ensure_utc(now)
    start = now - cfg.window
    scoped = [
        t for t in trades
        if t.contract.underlying == symbol.upper() and start <= t.known_at <= now
    ]
    enriched = enrich(scoped, now, cfg)

    bull = bear = total = signable = 0.0
    raw = sum(t.premium for t in scoped)
    sweeps = blocks = kept = 0
    opening_known = opening_true = 0

    for ft in enriched:
        if ft.reject:
            continue
        sp = ft.signed_premium
        if sp > 0:
            bull += sp
        else:
            bear += -sp
        total += abs(sp)
        signable += ft.trade.premium
        kept += 1
        if ft.is_sweep:
            sweeps += 1
        if ft.is_block:
            blocks += 1
        if ft.opening is not None:
            opening_known += 1
            opening_true += 1 if ft.opening else 0

    denom = bull + bear
    imbalance = (bull - bear) / denom if denom > 0 else 0.0

    notable = sorted(
        (f for f in enriched if not f.reject),
        key=lambda f: abs(f.signed_premium), reverse=True,
    )[:5]

    return FlowSummary(
        symbol=symbol.upper(),
        window_start=start,
        window_end=now,
        bullish_premium=bull,
        bearish_premium=bear,
        total_premium=total,
        signable_premium=signable,
        raw_premium=raw,
        imbalance=imbalance,
        trade_count=len(scoped),
        kept_count=kept,
        sweep_count=sweeps,
        block_count=blocks,
        opening_fraction=(opening_true / opening_known) if opening_known else None,
        top=tuple(
            {
                "strike": f.trade.contract.strike,
                "right": f.trade.contract.right.value,
                "expiry": f.trade.contract.expiry.strftime("%Y-%m-%d"),
                "premium": round(f.trade.premium, 2),
                "aggressor": f.aggressor.value,
                "structure": f.structure,
                "sweep": f.is_sweep,
                "opening": f.opening,
            }
            for f in notable
        ),
    )


def evaluate(
    symbol: str,
    trades: Sequence[OptionTrade],
    now: datetime,
    cfg: OptionsFlowConfig,
    baseline: Optional[FlowBaseline] = None,
) -> list[Evidence]:
    """Score the options tape into evidence.

    The score is a product of two independent conditions, and both must hold:

        imbalance    is the flow one-sided?
        unusualness  is there more of it than this symbol normally sees?

    Multiplying rather than adding is deliberate. Balanced flow of any size is
    not a signal, and a one-sided trickle on a quiet afternoon is not either.
    Only lopsided *and* abnormal clears.
    """
    now = ensure_utc(now)
    s = summarise(symbol, trades, now, cfg)

    if s.kept_count == 0 or s.signable_premium < cfg.min_premium_for_signal:
        return []

    # Unusualness is measured in raw dollars, not conviction-weighted ones, so
    # it answers "is there abnormally much activity here" independently of
    # "does that activity look informed" — the two must not be entangled or a
    # single sweep multiplier would make an ordinary day look abnormal.
    z = baseline.zscore(symbol, s.signable_premium) if baseline else None
    if z is None:
        # No baseline yet: fall back to an absolute yardstick at reduced
        # confidence rather than assuming the flow is unusual.
        unusualness = min(1.0, s.signable_premium / (cfg.min_premium_for_signal * 4))
        conf_base = 0.35
    else:
        unusualness = max(0.0, squash(z, cfg.zscore_significant))
        conf_base = 0.5

    score = clamp(s.imbalance * unusualness)

    conf = conf_base
    if s.sweep_count:
        conf += 0.15                     # urgency is the most informative attribute
    if s.block_count:
        conf += 0.08
    if s.opening_fraction is not None and s.opening_fraction > 0.6:
        conf += cfg.opening_confidence_bonus
    if s.kept_count < 3:
        conf *= 0.6                      # a couple of prints is an anecdote
    conf *= 0.5 + 0.5 * min(1.0, s.signable_fraction * 2)
    conf = clamp(conf, 0.0, 1.0)

    if conf < cfg.min_confidence or abs(score) < 0.05:
        return []

    return [Evidence(
        source=Source.OPTIONS_FLOW,
        kind="premium_imbalance",
        symbol=symbol.upper(),
        score=score,
        confidence=conf,
        observed_at=now,
        ttl=cfg.ttl,
        detail={
            "bullish_premium": round(s.bullish_premium),
            "bearish_premium": round(s.bearish_premium),
            "signable_premium": round(s.signable_premium),
            "imbalance": round(s.imbalance, 3),
            "zscore": round(z, 2) if z is not None else None,
            "unusualness": round(unusualness, 3),
            "trades_seen": s.trade_count,
            "trades_kept": s.kept_count,
            "signable_fraction": round(s.signable_fraction, 3),
            "sweeps": s.sweep_count,
            "blocks": s.block_count,
            "opening_fraction": (
                round(s.opening_fraction, 2) if s.opening_fraction is not None else None
            ),
            "top_trades": list(s.top),
        },
    )]


__all__ = [
    "FlowTrade", "FlowSummary", "FlowBaseline",
    "enrich", "summarise", "evaluate", "detect_sweeps", "detect_structures",
]
