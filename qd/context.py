"""
qd.context — regime detection. Layer 1 of 4, and a hard gate on the other three.

Most strategies work in one regime and lose money in the others. A trend
strategy tested across a decade reports the average of "excellent in 2020" and
"death by a thousand whipsaws in 2017", and that average tells you almost
nothing about what happens next — the number is a blend of two distributions
that never occur together.

So regime is separated out and classified BEFORE any signal is consulted, using
rules with no reference to the signal, and each strategy declares the regimes it
is allowed to trade. This layer can be tested entirely on its own: feed it bars,
check the label. No evidence, no positions, no broker.

Everything here is deliberately crude. Two measurements, fixed thresholds, three
labels. A finely-tuned regime classifier is a second strategy hiding inside the
filter, with its own overfitting and its own need for proof — and when the
system loses money you will not be able to tell which of the two failed.

THRESHOLDS ARE SET A PRIORI AND ARE NOT FOR OPTIMISING. They are round numbers
chosen from the definition of the measure, not from a sweep. Tuning the regime
filter against returns is the most efficient way to overfit a system, because it
silently selects the periods the signal happened to work in and calls the
selection a "market condition".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Sequence

from qd.types import Bar, ensure_utc


class Regime(str, Enum):
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    CHOP = "chop"
    UNKNOWN = "unknown"          # not enough history to say

    @property
    def is_trending(self) -> bool:
        return self in (Regime.TREND_UP, Regime.TREND_DOWN)


class VolState(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    EXTREME = "extreme"
    UNKNOWN = "unknown"

    @property
    def is_tradeable(self) -> bool:
        """EXTREME is excluded everywhere.

        In a volatility panic, correlations converge on 1, the sector caps stop
        meaning what they meant in the backtest, and stop distances scale to the
        point where position sizes become too small to matter anyway. The
        distribution of outcomes in that state is not the one the strategy was
        measured on.
        """
        return self is not VolState.EXTREME


# ─────────────────────────────────────────────────────────────────────────────
# Measures
# ─────────────────────────────────────────────────────────────────────────────

def efficiency_ratio(bars: Sequence[Bar], period: int = 20) -> Optional[float]:
    """Kaufman efficiency ratio: net travel divided by gross travel, in [0, 1].

        ER = |close_t − close_{t−n}| / Σ|close_i − close_{i−1}|

    A market that moves 10 points in a straight line scores near 1.0. One that
    moves 10 points up and 10 back down repeatedly scores near 0.

    Chosen over ADX or a moving-average slope because it needs no smoothing
    constant and no tuning: it is a ratio of two directly observable distances,
    so there is nothing in it to fit.
    """
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1):]
    net = abs(window[-1].close - window[0].close)
    gross = sum(
        abs(window[i].close - window[i - 1].close) for i in range(1, len(window))
    )
    if gross <= 0:
        return None
    return net / gross


def realized_vol(bars: Sequence[Bar], period: int = 20, annualize: int = 252) -> Optional[float]:
    """Annualised close-to-close volatility as a fraction (0.35 = 35%)."""
    if len(bars) < period + 1:
        return None
    window = bars[-(period + 1):]
    rets = []
    for i in range(1, len(window)):
        prev = window[i - 1].close
        if prev <= 0:
            continue
        rets.append(math.log(window[i].close / prev))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(annualize)


def percentile_rank(value: float, history: Sequence[float]) -> Optional[float]:
    """Where `value` sits in `history`, in [0, 1]."""
    if len(history) < 20:
        return None
    below = sum(1 for h in history if h < value)
    return below / len(history)


def sma(bars: Sequence[Bar], period: int) -> Optional[float]:
    if len(bars) < period:
        return None
    return sum(b.close for b in bars[-period:]) / period


# ─────────────────────────────────────────────────────────────────────────────
# Classification
# ─────────────────────────────────────────────────────────────────────────────

# Fixed. See the module docstring — these are not parameters.
ER_TRENDING = 0.30          # above this, net travel dominates gross travel
VOL_LOW_PCTL = 0.25
VOL_HIGH_PCTL = 0.75
VOL_EXTREME_PCTL = 0.95
MIN_BARS = 60               # enough for a 50-period SMA plus the ER window


@dataclass(frozen=True)
class ContextState:
    """The regime label for one symbol (or for the market) at one instant."""
    symbol: str
    now: datetime
    regime: Regime
    vol_state: VolState
    efficiency: Optional[float]
    vol_annual: Optional[float]
    vol_percentile: Optional[float]
    sma_fast: Optional[float]
    sma_slow: Optional[float]
    bars_used: int

    @property
    def known(self) -> bool:
        return self.regime is not Regime.UNKNOWN

    def permits(
        self,
        allowed_regimes: Sequence[Regime],
        require_known: bool = True,
    ) -> tuple[bool, str]:
        """Whether a strategy declaring `allowed_regimes` may trade here."""
        if not self.known:
            return (False, "regime unknown (insufficient history)") if require_known else (True, "")
        if not self.vol_state.is_tradeable:
            return False, f"volatility {self.vol_state.value}"
        if self.regime not in allowed_regimes:
            allowed = "/".join(r.value for r in allowed_regimes)
            return False, f"regime {self.regime.value}, strategy needs {allowed}"
        return True, ""

    def describe(self) -> str:
        er = f"{self.efficiency:.3f}" if self.efficiency is not None else "n/a"
        vp = f"{self.vol_percentile:.0%}" if self.vol_percentile is not None else "n/a"
        va = f"{self.vol_annual:.1%}" if self.vol_annual is not None else "n/a"
        return (
            f"{self.symbol} regime={self.regime.value} vol={self.vol_state.value} "
            f"(ER={er} rv={va} pctl={vp})"
        )


def classify(
    symbol: str,
    daily_bars: Sequence[Bar],
    now: datetime,
    er_period: int = 20,
    vol_period: int = 20,
    vol_history_days: int = 252,
) -> ContextState:
    """Label the regime from daily bars.

    Only bars that had CLOSED at `now` are used — the same point-in-time rule as
    everywhere else. A regime filter that peeks is worse than none, because it
    silently selects the periods the signal worked in and presents that
    selection as a market condition.
    """
    now = ensure_utc(now)
    bars = [b for b in daily_bars if b.known_at <= now]

    if len(bars) < MIN_BARS:
        return ContextState(
            symbol=symbol.upper(), now=now, regime=Regime.UNKNOWN,
            vol_state=VolState.UNKNOWN, efficiency=None, vol_annual=None,
            vol_percentile=None, sma_fast=None, sma_slow=None, bars_used=len(bars),
        )

    er = efficiency_ratio(bars, er_period)
    fast = sma(bars, 20)
    slow = sma(bars, 50)
    rv = realized_vol(bars, vol_period)

    # Volatility percentile against this symbol's own trailing year. Absolute
    # volatility is not comparable across names — 40% annualised is calm for a
    # biotech and a crisis for a utility.
    #
    # Log returns are computed ONCE and the rolling window slides over them.
    # The obvious implementation — calling realized_vol() on each growing
    # prefix — recomputes every return from scratch at every step, which is
    # quadratic and, called per symbol per cycle, is slow enough to stall a
    # backtest outright.
    vol_pctl: Optional[float] = None
    if rv is not None:
        span = bars[-min(len(bars), vol_history_days + vol_period):]
        rets: list[float] = []
        for i in range(1, len(span)):
            prev = span[i - 1].close
            if prev > 0:
                rets.append(math.log(span[i].close / prev))
        history: list[float] = []
        if len(rets) >= vol_period:
            ann = math.sqrt(252)
            for end in range(vol_period, len(rets) + 1):
                window = rets[end - vol_period: end]
                mean = sum(window) / len(window)
                var = sum((r - mean) ** 2 for r in window) / (len(window) - 1)
                history.append(math.sqrt(var) * ann)
        vol_pctl = percentile_rank(rv, history)

    vol_state = VolState.UNKNOWN
    if vol_pctl is not None:
        if vol_pctl >= VOL_EXTREME_PCTL:
            vol_state = VolState.EXTREME
        elif vol_pctl >= VOL_HIGH_PCTL:
            vol_state = VolState.HIGH
        elif vol_pctl <= VOL_LOW_PCTL:
            vol_state = VolState.LOW
        else:
            vol_state = VolState.NORMAL

    # Trend requires BOTH conditions: efficient travel and moving averages in
    # order. Either alone is too easy to satisfy — a market can grind sideways
    # with the averages stacked, or spike once and score a high ER on what is
    # really a single gap.
    regime = Regime.CHOP
    if er is not None and fast is not None and slow is not None:
        if er >= ER_TRENDING:
            if fast > slow and bars[-1].close > slow:
                regime = Regime.TREND_UP
            elif fast < slow and bars[-1].close < slow:
                regime = Regime.TREND_DOWN

    return ContextState(
        symbol=symbol.upper(), now=now, regime=regime, vol_state=vol_state,
        efficiency=er, vol_annual=rv, vol_percentile=vol_pctl,
        sma_fast=fast, sma_slow=slow, bars_used=len(bars),
    )


@dataclass(frozen=True)
class MarketContext:
    """Regime for the index plus the symbol, combined.

    A long single-name position in a market-wide downtrend is fighting the
    factor that explains most of its return. Single-name context alone misses
    that: a stock can look individually fine while beta drags it down.
    """
    market: ContextState
    symbol: ContextState

    def permits(
        self,
        allowed_regimes: Sequence[Regime],
        market_regimes: Optional[Sequence[Regime]] = None,
    ) -> tuple[bool, str]:
        ok, why = self.symbol.permits(allowed_regimes)
        if not ok:
            return False, f"symbol: {why}"
        if market_regimes is not None:
            # The market's regime label may legitimately be unknown early in a
            # backtest; only its volatility state is enforced unconditionally.
            if not self.market.vol_state.is_tradeable:
                return False, f"market volatility {self.market.vol_state.value}"
            if self.market.known and self.market.regime not in market_regimes:
                allowed = "/".join(r.value for r in market_regimes)
                return False, f"market regime {self.market.regime.value}, need {allowed}"
        return True, ""

    def describe(self) -> str:
        return f"[mkt {self.market.describe()}] [{self.symbol.describe()}]"


__all__ = [
    "Regime", "VolState", "ContextState", "MarketContext", "classify",
    "efficiency_ratio", "realized_vol", "percentile_rank", "sma",
    "ER_TRENDING", "VOL_LOW_PCTL", "VOL_HIGH_PCTL", "VOL_EXTREME_PCTL", "MIN_BARS",
]
