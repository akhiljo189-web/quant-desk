"""
qd.features.market — price and volume structure, channel 1 of 4.

Indicators are computed only from bars that had already closed at the decision
instant. `BarSeries.visible_at()` enforces it, and every public function here
takes the visible slice rather than the raw history, so a look-ahead bug has to
get past an explicit filter instead of merely being forgotten.

Nothing in this module predicts anything on its own. It measures four things —
trend alignment, participation, position within the day's value area, and the
overnight gap — and hands them to the strategy layer as scored evidence. The
market channel exists mainly to answer "is price behaving consistently with the
story the other channels are telling", which is a much weaker claim than
"price is going up" and a much more defensible one.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from qd.clock import CALENDAR, ET, MarketCalendar
from qd.config import MarketConfig
from qd.types import Bar, Evidence, Source, ensure_utc, squash

# ─────────────────────────────────────────────────────────────────────────────
# Series container
# ─────────────────────────────────────────────────────────────────────────────

class BarSeries:
    """Time-ordered bars for one symbol, with point-in-time slicing."""

    __slots__ = ("symbol", "_bars")

    def __init__(self, symbol: str, bars: Iterable[Bar] = ()) -> None:
        self.symbol = symbol.upper()
        self._bars: list[Bar] = sorted(bars, key=lambda b: b.end)

    def __len__(self) -> int:
        return len(self._bars)

    def __iter__(self):
        return iter(self._bars)

    def __getitem__(self, i):
        return self._bars[i]

    def append(self, bar: Bar) -> None:
        """Append, keeping order. Duplicate bar ends replace rather than stack:
        providers re-send the most recent bar as it finalises, and letting both
        copies through double-counts its volume."""
        if self._bars and bar.end == self._bars[-1].end:
            self._bars[-1] = bar
            return
        if self._bars and bar.end < self._bars[-1].end:
            self._bars.append(bar)
            self._bars.sort(key=lambda b: b.end)
            return
        self._bars.append(bar)

    def visible_at(self, now: datetime) -> list[Bar]:
        """Bars that had closed at `now`. The point-in-time boundary."""
        now = ensure_utc(now)
        return [b for b in self._bars if b.known_at <= now]

    def last_visible(self, now: datetime) -> Optional[Bar]:
        vis = self.visible_at(now)
        return vis[-1] if vis else None

    def session_bars(self, now: datetime, cal: MarketCalendar = CALENDAR) -> list[Bar]:
        """Visible bars belonging to the current regular session."""
        bounds = cal.session_bounds(ensure_utc(now).astimezone(ET).date())
        if bounds is None:
            return []
        open_, close_ = bounds
        return [b for b in self.visible_at(now) if open_ <= b.start < close_]

    def trim(self, keep: int = 500) -> None:
        """Bound memory in a long-running process."""
        if len(self._bars) > keep:
            self._bars = self._bars[-keep:]


# ─────────────────────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────────────────────

def true_range(bar: Bar, prev_close: Optional[float]) -> float:
    if prev_close is None:
        return bar.range
    return max(
        bar.high - bar.low,
        abs(bar.high - prev_close),
        abs(bar.low - prev_close),
    )


def atr(bars: Sequence[Bar], period: int = 14) -> Optional[float]:
    """Wilder's ATR. None when there is not enough history.

    Returning None rather than a partial average matters: a "warm-up" ATR
    computed from four bars is small, and a small ATR produces a tight stop and
    therefore a large position. The failure mode of guessing here is maximum
    size on minimum information.
    """
    if len(bars) < period + 1:
        return None
    trs = [true_range(bars[i], bars[i - 1].close) for i in range(1, len(bars))]
    if len(trs) < period:
        return None
    val = sum(trs[:period]) / period
    for tr in trs[period:]:
        val = (val * (period - 1) + tr) / period
    return val


def ema(values: Sequence[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    val = sum(values[:period]) / period
    for v in values[period:]:
        val = v * k + val * (1 - k)
    return val


def session_vwap(bars: Sequence[Bar]) -> Optional[float]:
    """Volume-weighted average price across the given bars."""
    num = den = 0.0
    for b in bars:
        px = b.vwap if b.vwap is not None else b.typical
        num += px * b.volume
        den += b.volume
    return num / den if den > 0 else None


def stdev(values: Sequence[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (n - 1))


def relative_volume(
    session_volume: float,
    baseline_daily_volume: float,
    elapsed_fraction: float,
) -> Optional[float]:
    """Today's pace versus a normal day, corrected for time of day.

    Comparing raw cumulative volume to a full-day average makes every morning
    look quiet and every afternoon look busy. Dividing by elapsed session
    fraction removes that. The floor on `elapsed_fraction` avoids the first
    seconds of the session dividing by ~0 and reporting infinite conviction.
    """
    if baseline_daily_volume <= 0:
        return None
    frac = max(0.02, min(1.0, elapsed_fraction))
    expected = baseline_daily_volume * frac
    return session_volume / expected if expected > 0 else None


def average_daily_volume(daily_bars: Sequence[Bar], days: int = 20) -> Optional[float]:
    if not daily_bars:
        return None
    recent = daily_bars[-days:]
    return sum(b.volume for b in recent) / len(recent) if recent else None


def average_dollar_volume(daily_bars: Sequence[Bar], days: int = 20) -> Optional[float]:
    if not daily_bars:
        return None
    recent = daily_bars[-days:]
    if not recent:
        return None
    return sum(b.volume * b.close for b in recent) / len(recent)


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketSnapshot:
    """Everything the rest of the system needs to know about price right now.

    Also the tradability gate: `atr_pct` and `adv_dollar` decide whether a name
    is liquid enough to trade at all, which filters out more bad trades than
    any signal threshold does.
    """
    symbol: str
    now: datetime
    last: float
    atr: Optional[float]
    atr_pct: Optional[float]          # ATR as % of price — comparable across names
    vwap: Optional[float]
    ema_fast: Optional[float]
    ema_slow: Optional[float]
    rvol: Optional[float]
    gap_pct: Optional[float]
    prev_close: Optional[float]
    session_high: Optional[float]
    session_low: Optional[float]
    adv_dollar: Optional[float]
    bar_count: int
    last_bar_end: Optional[datetime]

    @property
    def has_core(self) -> bool:
        """Whether the essential inputs exist. Missing ATR means no stop
        distance, and no stop distance means no trade."""
        return self.atr is not None and self.atr > 0 and self.last > 0

    def stale_by(self, now: datetime) -> Optional[timedelta]:
        if self.last_bar_end is None:
            return None
        return ensure_utc(now) - self.last_bar_end

    def range_position(self) -> Optional[float]:
        """Where price sits in the session range: 0 = low, 1 = high."""
        if self.session_high is None or self.session_low is None:
            return None
        span = self.session_high - self.session_low
        if span <= 0:
            return None
        return (self.last - self.session_low) / span


def build_snapshot(
    symbol: str,
    intraday: BarSeries,
    daily: BarSeries,
    now: datetime,
    cfg: MarketConfig,
    cal: MarketCalendar = CALENDAR,
) -> MarketSnapshot:
    """Compute a snapshot from bars visible at `now`."""
    now = ensure_utc(now)
    vis = intraday.visible_at(now)
    dvis = daily.visible_at(now)
    sess = intraday.session_bars(now, cal)

    last_bar = vis[-1] if vis else None
    last = last_bar.close if last_bar else 0.0
    closes = [b.close for b in vis]

    # ATR from daily bars when available — an intraday ATR on 5-minute bars
    # measures 5-minute noise, and sizing a multi-hour hold off it produces a
    # stop that any ordinary wiggle takes out.
    atr_val = atr(dvis, cfg.atr_period) if len(dvis) > cfg.atr_period else atr(vis, cfg.atr_period)

    prev_close = dvis[-1].close if dvis else None
    gap = None
    if prev_close and sess:
        gap = (sess[0].open - prev_close) / prev_close * 100.0

    baseline = average_daily_volume(dvis, cfg.rvol_lookback_days)
    rv = relative_volume(
        sum(b.volume for b in sess), baseline or 0.0, cal.elapsed_fraction(now)
    )

    return MarketSnapshot(
        symbol=symbol.upper(),
        now=now,
        last=last,
        atr=atr_val,
        atr_pct=(atr_val / last * 100.0) if atr_val and last > 0 else None,
        vwap=session_vwap(sess),
        ema_fast=ema(closes, cfg.trend_fast),
        ema_slow=ema(closes, cfg.trend_slow),
        rvol=rv,
        gap_pct=gap,
        prev_close=prev_close,
        session_high=max((b.high for b in sess), default=None),
        session_low=min((b.low for b in sess), default=None),
        adv_dollar=average_dollar_volume(dvis, cfg.rvol_lookback_days),
        bar_count=len(vis),
        last_bar_end=last_bar.end if last_bar else None,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Evidence
# ─────────────────────────────────────────────────────────────────────────────

def evaluate(
    snap: MarketSnapshot,
    intraday: BarSeries,
    cfg: MarketConfig,
    cal: MarketCalendar = CALENDAR,
) -> list[Evidence]:
    """Turn a snapshot into scored evidence.

    Each reading is separate rather than pre-blended, so the strategy layer can
    weigh them and the journal can show which one actually drove a trade.
    """
    out: list[Evidence] = []
    if not snap.has_core or snap.bar_count < cfg.min_bars_for_signal:
        return out

    now, price, atr_val = snap.now, snap.last, snap.atr or 0.0

    # ── Trend alignment ──────────────────────────────────────────────────────
    # Fast/slow EMA separation measured in ATR, so a 2-point spread means the
    # same thing in a $30 stock and a $600 one.
    if snap.ema_fast is not None and snap.ema_slow is not None and atr_val > 0:
        sep = (snap.ema_fast - snap.ema_slow) / atr_val
        score = squash(sep, 0.5)
        # Price on the wrong side of its own fast EMA contradicts the trend
        # reading; halve confidence rather than invert the score, because a
        # pullback within a trend is not a reversal.
        aligned = (price > snap.ema_fast) == (score > 0)
        out.append(Evidence(
            source=Source.MARKET,
            kind="trend_alignment",
            symbol=snap.symbol,
            score=score,
            confidence=0.65 if aligned else 0.30,
            observed_at=now,
            ttl=cfg.ttl,
            detail={
                "ema_fast": round(snap.ema_fast, 4),
                "ema_slow": round(snap.ema_slow, 4),
                "separation_atr": round(sep, 3),
                "price_aligned": aligned,
            },
        ))

    # ── Participation ────────────────────────────────────────────────────────
    # Volume is not directional by itself; it is a confidence multiplier on the
    # direction the recent bars are already showing. High volume on a doji says
    # only that people disagreed loudly.
    if snap.rvol is not None and snap.rvol > 0:
        recent = intraday.visible_at(now)[-6:]
        if len(recent) >= 3:
            net = sum((b.close - b.open) for b in recent)
            span = sum(b.range for b in recent) or 1.0
            direction = max(-1.0, min(1.0, net / span))
            magnitude = squash(snap.rvol - 1.0, cfg.rvol_significant - 1.0)
            body_quality = sum(b.body_pct for b in recent) / len(recent)
            out.append(Evidence(
                source=Source.MARKET,
                kind="volume_thrust",
                symbol=snap.symbol,
                score=direction * abs(magnitude),
                confidence=min(0.75, 0.25 + body_quality * 0.6),
                observed_at=now,
                ttl=cfg.ttl,
                detail={
                    "rvol": round(snap.rvol, 2),
                    "net_direction": round(direction, 3),
                    "avg_body_pct": round(body_quality, 3),
                    "bars": len(recent),
                },
            ))

    # ── Value-area extension ─────────────────────────────────────────────────
    # Distance from VWAP in ATR units. Read as *extension*, not momentum: far
    # above VWAP on strong evidence is confirmation, but far above VWAP is also
    # where chasing gets punished, so confidence falls as the stretch grows.
    if snap.vwap is not None and atr_val > 0:
        dist = (price - snap.vwap) / atr_val
        stretched = abs(dist) > 2.0
        out.append(Evidence(
            source=Source.MARKET,
            kind="vwap_extension",
            symbol=snap.symbol,
            score=squash(dist, cfg.vwap_dist_significant_atr),
            confidence=0.20 if stretched else 0.50,
            observed_at=now,
            ttl=cfg.ttl,
            detail={
                "vwap": round(snap.vwap, 4),
                "distance_atr": round(dist, 3),
                "stretched": stretched,
            },
        ))

    # ── Opening gap ──────────────────────────────────────────────────────────
    # Only meaningful early. By late morning the gap has either filled or been
    # accepted, and either way it is no longer news.
    if snap.gap_pct is not None and abs(snap.gap_pct) >= 0.5:
        elapsed = cal.minutes_since_open(now)
        if elapsed is not None and 0 <= elapsed <= 90:
            holding = None
            if snap.prev_close:
                # Is price holding the gap, or giving it back?
                holding = (price - snap.prev_close) / snap.prev_close * 100.0
            follow = 0.0
            if holding is not None and snap.gap_pct != 0:
                follow = max(0.0, min(1.5, holding / snap.gap_pct))
            out.append(Evidence(
                source=Source.MARKET,
                kind="gap_continuation",
                symbol=snap.symbol,
                score=squash(snap.gap_pct, cfg.gap_significant_pct) * min(1.0, follow),
                confidence=0.45 * max(0.0, 1.0 - elapsed / 90.0) + 0.15,
                observed_at=now,
                ttl=cfg.ttl,
                detail={
                    "gap_pct": round(snap.gap_pct, 3),
                    "held_pct": round(holding, 3) if holding is not None else None,
                    "follow_through": round(follow, 3),
                    "minutes_since_open": round(elapsed, 1),
                },
            ))

    return out


def is_tradeable(snap: MarketSnapshot, universe, spread_bps: Optional[float] = None) -> tuple[bool, str]:
    """Liquidity and sanity gate. Returns (ok, reason).

    Runs before any signal is considered. Rejecting here is cheap; discovering
    at the fill that a name trades 40bps wide is not.
    """
    if not snap.has_core:
        return False, "insufficient data (no ATR or price)"
    if snap.last < universe.min_price:
        return False, f"price {snap.last:.2f} below min {universe.min_price}"
    if snap.last > universe.max_price:
        return False, f"price {snap.last:.2f} above max {universe.max_price}"
    if snap.adv_dollar is not None and snap.adv_dollar < universe.min_avg_dollar_volume:
        return False, f"ADV ${snap.adv_dollar:,.0f} below ${universe.min_avg_dollar_volume:,.0f}"
    if snap.atr_pct is not None:
        if snap.atr_pct < universe.min_atr_pct:
            return False, f"ATR {snap.atr_pct:.2f}% too quiet"
        if snap.atr_pct > universe.max_atr_pct:
            return False, f"ATR {snap.atr_pct:.2f}% too volatile"
    if spread_bps is not None and spread_bps > universe.max_spread_bps:
        return False, f"spread {spread_bps:.1f}bps above {universe.max_spread_bps}bps"
    return True, "ok"


__all__ = [
    "BarSeries", "MarketSnapshot", "build_snapshot", "evaluate", "is_tradeable",
    "atr", "ema", "session_vwap", "relative_volume", "true_range",
    "average_daily_volume", "average_dollar_volume", "stdev",
]
