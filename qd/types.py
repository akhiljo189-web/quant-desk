"""
qd.types — the record types that flow through the system.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE
------------------------------------------
Every observation carries two timestamps:

    event_time  — when the thing happened in the world
    known_at    — the first moment THIS SYSTEM could have acted on it

They are never the same and the gap is never zero. A 16:05 earnings release is
an `event_time`; if the feed hands it to us at 16:05:31 then `known_at` is
16:05:31 and any backtest that lets us trade it at 16:05:00 is lying. A daily
bar's `event_time` is the session it covers; its `known_at` is the moment the
bar closed, not the moment it opened.

Backtests fail in exactly one direction — they use information that had not
arrived yet — and they fail silently, printing a beautiful equity curve. So the
defence is structural rather than a matter of care: the replay provider indexes
purely on `known_at` and physically cannot serve a record whose `known_at` is in
the simulated future. `tests/test_pointintime.py` asserts it.

Records are frozen. Nothing downstream mutates an observation after it lands.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Iterable, Mapping, Optional, Sequence

UTC = timezone.utc


def utcnow() -> datetime:
    """Timezone-aware now. Never use datetime.utcnow() — it returns a naive
    datetime that compares wrongly against every aware timestamp in here."""
    return datetime.now(UTC)


def ensure_utc(ts: datetime) -> datetime:
    """Coerce to an aware UTC datetime; reject naive input loudly.

    A naive datetime slipping into the point-in-time index is precisely how
    look-ahead gets in, so this raises rather than guessing a zone.
    """
    if ts.tzinfo is None:
        raise ValueError(
            f"naive datetime {ts!r} — every timestamp in qd must be timezone-aware"
        )
    return ts.astimezone(UTC)


# ─────────────────────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────────────────────

class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class Right(str, Enum):
    """Option right."""
    CALL = "C"
    PUT = "P"


class Aggressor(str, Enum):
    """Which side of the book initiated a trade, inferred from the NBBO."""
    BUY = "buy"          # traded at or above the ask — buyer lifted the offer
    SELL = "sell"        # traded at or below the bid — seller hit the bid
    MID = "mid"          # inside the spread — negotiated, direction unclear
    UNKNOWN = "unknown"  # no usable quote at trade time


class Source(str, Enum):
    """The four evidence channels."""
    MARKET = "market"
    NEWS = "news"
    EARNINGS = "earnings"
    OPTIONS_FLOW = "options_flow"


# ─────────────────────────────────────────────────────────────────────────────
# Observations
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Bar:
    """An OHLCV bar. `known_at` is the bar's CLOSE, never its open.

    The single most expensive bug in retail backtesting is stamping a bar with
    its start time and then treating indicators derived from its close as
    available at that start. A 4-hour bar stamped 12:00 whose EMA you read at
    12:00 gives you four hours of foresight per bar and an equity curve that
    cannot be traded.
    """
    symbol: str
    start: datetime
    end: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: Optional[float] = None
    trades: Optional[int] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", ensure_utc(self.start))
        object.__setattr__(self, "end", ensure_utc(self.end))
        if self.end <= self.start:
            raise ValueError(f"{self.symbol}: bar end {self.end} <= start {self.start}")

    @property
    def event_time(self) -> datetime:
        return self.start

    @property
    def known_at(self) -> datetime:
        return self.end

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def typical(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def body_pct(self) -> float:
        """Body as a fraction of full range — conviction within the bar."""
        return self.body / self.range if self.range > 0 else 0.0

    def close_position(self) -> float:
        """Where the close sits in the bar's range: 0.0 = low, 1.0 = high."""
        return (self.close - self.low) / self.range if self.range > 0 else 0.5


@dataclass(frozen=True, slots=True)
class Quote:
    """An NBBO snapshot."""
    symbol: str
    ts: datetime
    bid: float
    ask: float
    bid_size: float = 0.0
    ask_size: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts))

    @property
    def event_time(self) -> datetime:
        return self.ts

    @property
    def known_at(self) -> datetime:
        return self.ts

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.spread / m) * 10_000.0 if m > 0 else float("inf")

    @property
    def is_crossed(self) -> bool:
        """Bid above ask — a stale or broken quote. Never trade on one."""
        return self.bid > self.ask

    @property
    def is_valid(self) -> bool:
        return self.bid > 0 and self.ask > 0 and not self.is_crossed


@dataclass(frozen=True, slots=True)
class NewsItem:
    """A headline. `published_at` is the wire time; `known_at` is when it
    reached us, which is later and is what we are allowed to trade on."""
    id: str
    symbols: tuple[str, ...]
    headline: str
    published_at: datetime
    received_at: datetime
    source: str = ""
    summary: str = ""
    url: str = ""
    # Classification is cached ON the record at ingest, never recomputed at
    # decision time. See features/news.py for why that matters.
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "published_at", ensure_utc(self.published_at))
        object.__setattr__(self, "received_at", ensure_utc(self.received_at))
        object.__setattr__(self, "symbols", tuple(s.upper() for s in self.symbols))

    @property
    def event_time(self) -> datetime:
        return self.published_at

    @property
    def known_at(self) -> datetime:
        # Wire time can lag or lead our receipt; only receipt is defensible.
        return max(self.received_at, self.published_at)

    @property
    def latency(self) -> timedelta:
        return self.received_at - self.published_at

    @property
    def text(self) -> str:
        return f"{self.headline}\n{self.summary}".strip()

    def dedup_key(self) -> str:
        """Stable hash of normalised headline text, for novelty detection.

        The fifth rewrite of a story is not new information, but it arrives
        looking exactly like the first one.
        """
        norm = " ".join(self.headline.lower().split())
        return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True, slots=True)
class EarningsEvent:
    """A scheduled or released earnings report.

    `known_at` is deliberately NOT the report date. A company reporting after
    the close on the 5th has estimates known days earlier and actuals known at
    roughly 16:05 on the 5th. Treating the whole record as known on the report
    date hands the backtest the actual EPS before the release — the classic
    fundamental-data leak.
    """
    symbol: str
    report_date: datetime          # scheduled session date (date at 00:00 UTC)
    session: str                   # "bmo" (before open) | "amc" (after close) | "dmt"
    scheduled_known_at: datetime   # when the SCHEDULE became known
    eps_estimate: Optional[float] = None
    eps_actual: Optional[float] = None
    revenue_estimate: Optional[float] = None
    revenue_actual: Optional[float] = None
    released_at: Optional[datetime] = None   # when ACTUALS hit the wire
    fiscal_period: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_date", ensure_utc(self.report_date))
        object.__setattr__(self, "scheduled_known_at", ensure_utc(self.scheduled_known_at))
        if self.released_at is not None:
            object.__setattr__(self, "released_at", ensure_utc(self.released_at))

    @property
    def event_time(self) -> datetime:
        return self.released_at or self.report_date

    @property
    def known_at(self) -> datetime:
        """When the *schedule* became known. Actuals have their own gate below."""
        return self.scheduled_known_at

    def actuals_known_at(self) -> Optional[datetime]:
        """When the numbers became actionable, or None if not yet released."""
        if self.eps_actual is None and self.revenue_actual is None:
            return None
        return self.released_at

    def has_actuals_at(self, now: datetime) -> bool:
        ka = self.actuals_known_at()
        return ka is not None and ka <= ensure_utc(now)

    def eps_surprise_pct(self) -> Optional[float]:
        """Surprise as a fraction of |estimate|.

        Guarded against a near-zero estimate, where the percentage explodes to
        meaningless magnitudes — a 1c beat on a 0.1c estimate is not a 900%
        signal, it is a rounding artefact.
        """
        if self.eps_actual is None or self.eps_estimate is None:
            return None
        denom = abs(self.eps_estimate)
        if denom < 0.01:
            return None
        return (self.eps_actual - self.eps_estimate) / denom

    def revenue_surprise_pct(self) -> Optional[float]:
        if self.revenue_actual is None or self.revenue_estimate is None:
            return None
        if abs(self.revenue_estimate) < 1.0:
            return None
        return (self.revenue_actual - self.revenue_estimate) / abs(self.revenue_estimate)


@dataclass(frozen=True, slots=True)
class OptionContract:
    underlying: str
    expiry: datetime
    strike: float
    right: Right
    occ_symbol: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "expiry", ensure_utc(self.expiry))
        object.__setattr__(self, "underlying", self.underlying.upper())

    def dte(self, now: datetime) -> float:
        """Calendar days to expiry — fractional, so 0DTE reads below 1."""
        return max(0.0, (self.expiry - ensure_utc(now)).total_seconds() / 86400.0)

    def moneyness(self, spot: float) -> float:
        """(strike - spot) / spot, signed so positive is above spot.

        Combined with `right` this gives OTM/ITM: a call with positive
        moneyness is out of the money, a put with positive moneyness is in it.
        """
        return (self.strike - spot) / spot if spot > 0 else 0.0

    def is_otm(self, spot: float) -> bool:
        return self.strike > spot if self.right is Right.CALL else self.strike < spot


@dataclass(frozen=True, slots=True)
class OptionTrade:
    """A print on the options tape, with the NBBO that stood at the time.

    The quote matters more than the trade. Without knowing where the trade
    printed relative to the bid and ask you cannot tell a buyer paying up from
    a seller hitting a bid, and unsigned option volume is close to worthless as
    a directional read.
    """
    contract: OptionContract
    ts: datetime
    price: float
    size: int
    exchange: str = ""
    conditions: tuple[str, ...] = ()
    nbbo_bid: Optional[float] = None
    nbbo_ask: Optional[float] = None
    underlying_price: Optional[float] = None
    open_interest: Optional[int] = None     # OI at prior session close
    received_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts))
        if self.received_at is not None:
            object.__setattr__(self, "received_at", ensure_utc(self.received_at))

    @property
    def event_time(self) -> datetime:
        return self.ts

    @property
    def known_at(self) -> datetime:
        return self.received_at or self.ts

    @property
    def premium(self) -> float:
        """Dollar premium. US equity options are 100 shares per contract."""
        return self.price * self.size * 100.0

    @property
    def nbbo_mid(self) -> Optional[float]:
        if self.nbbo_bid is None or self.nbbo_ask is None:
            return None
        return (self.nbbo_bid + self.nbbo_ask) / 2.0

    def aggressor(self, tolerance: float = 0.0) -> Aggressor:
        """Classify who initiated, from trade price versus the NBBO.

        `tolerance` (in dollars) absorbs sub-penny quote drift so a trade a
        hair below the ask still reads as a lift rather than falling through to
        MID. Trades inside the spread stay MID: they are genuinely ambiguous,
        commonly negotiated or multi-leg, and guessing their direction is how
        flow readers manufacture conviction out of noise.
        """
        if self.nbbo_bid is None or self.nbbo_ask is None:
            return Aggressor.UNKNOWN
        if self.nbbo_ask <= 0 or self.nbbo_bid <= 0 or self.nbbo_bid > self.nbbo_ask:
            return Aggressor.UNKNOWN
        if self.price >= self.nbbo_ask - tolerance:
            return Aggressor.BUY
        if self.price <= self.nbbo_bid + tolerance:
            return Aggressor.SELL
        return Aggressor.MID

    def opens_position_likely(self) -> Optional[bool]:
        """True when trade size exceeds prior open interest on the contract.

        If someone trades 5,000 contracts where only 400 were outstanding, the
        position is being opened, not closed — new risk, not an exit. Without
        OI we return None rather than assuming; assuming "opening" is how every
        closing trade gets mistaken for a fresh institutional bet.
        """
        if self.open_interest is None:
            return None
        return self.size > self.open_interest


# ─────────────────────────────────────────────────────────────────────────────
# Evidence — the common currency of the four channels
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Evidence:
    """One scored, dated, attributable reading from one channel.

    Every channel — bars, headlines, earnings, options tape — reduces to this
    so the strategy layer can combine them without knowing anything about their
    origins. Three fields carry the weight:

      score      directional, [-1, +1]; sign is the trade direction it argues for
      confidence how much to trust this particular reading, [0, 1]
      ttl        how long it stays meaningful; after that it is discarded, not faded

    `detail` carries the raw numbers behind the score. It is not decoration —
    when a trade goes wrong the journal replays `detail` to show exactly which
    input produced the conviction.
    """
    source: Source
    kind: str
    symbol: str
    score: float
    confidence: float
    observed_at: datetime
    ttl: timedelta
    detail: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "observed_at", ensure_utc(self.observed_at))
        object.__setattr__(self, "score", _clamp(self.score, -1.0, 1.0))
        object.__setattr__(self, "confidence", _clamp(self.confidence, 0.0, 1.0))
        object.__setattr__(self, "symbol", self.symbol.upper())

    @property
    def expires_at(self) -> datetime:
        return self.observed_at + self.ttl

    def age(self, now: datetime) -> timedelta:
        return ensure_utc(now) - self.observed_at

    def is_live(self, now: datetime) -> bool:
        return ensure_utc(now) < self.expires_at

    def decayed_score(self, now: datetime, half_life: Optional[timedelta] = None) -> float:
        """Score after exponential time decay, 0.0 once expired.

        Default half-life is a third of the TTL, so a reading has lost most of
        its weight well before it formally expires. Information does not stay
        fresh until a cliff and then vanish; it bleeds out continuously, and a
        breaking headline is worth far less on its second hour than its first.
        """
        now = ensure_utc(now)
        if now >= self.expires_at:
            return 0.0
        hl = half_life or (self.ttl / 3)
        if hl.total_seconds() <= 0:
            return self.score
        elapsed = (now - self.observed_at).total_seconds()
        return self.score * math.pow(0.5, elapsed / hl.total_seconds())

    def weight(self, now: datetime, half_life: Optional[timedelta] = None) -> float:
        """Decayed score scaled by confidence — what the strategy actually sums."""
        return self.decayed_score(now, half_life) * self.confidence


# ─────────────────────────────────────────────────────────────────────────────
# Decisions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class Intent:
    """A proposed trade, before risk sizing. Carries its own justification.

    An Intent with an empty `evidence` tuple is unreachable by construction:
    the strategy builds these only from the readings that crossed threshold, so
    every trade this system places can name the inputs that caused it.
    """
    symbol: str
    side: Side
    conviction: float              # [0, 1], the combined evidence strength
    reference_price: float         # price the decision was made against
    stop_price: float
    target_price: float
    created_at: datetime
    evidence: tuple[Evidence, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "created_at", ensure_utc(self.created_at))
        object.__setattr__(self, "symbol", self.symbol.upper())
        if self.stop_price <= 0 or self.reference_price <= 0:
            raise ValueError(f"{self.symbol}: non-positive price in intent")
        # A stop on the wrong side of entry is not a stop; it fills instantly
        # and inverts the position's risk.
        if self.side is Side.BUY and self.stop_price >= self.reference_price:
            raise ValueError(
                f"{self.symbol}: long stop {self.stop_price} at/above entry "
                f"{self.reference_price}"
            )
        if self.side is Side.SELL and self.stop_price <= self.reference_price:
            raise ValueError(
                f"{self.symbol}: short stop {self.stop_price} at/below entry "
                f"{self.reference_price}"
            )

    @property
    def stop_distance(self) -> float:
        return abs(self.reference_price - self.stop_price)

    @property
    def reward_risk(self) -> float:
        r = self.stop_distance
        return abs(self.target_price - self.reference_price) / r if r > 0 else 0.0

    def sources(self) -> frozenset[Source]:
        return frozenset(e.source for e in self.evidence)

    def idempotency_key(self) -> str:
        """Deterministic ID for the broker's client_order_id.

        Restarts are the dangerous moment: the engine reboots, re-derives the
        same intent from the same data, and submits a duplicate. Bucketing to
        the minute means a re-derived intent reuses its key and the broker
        rejects the second copy instead of doubling the position.
        """
        stamp = self.created_at.strftime("%Y%m%dT%H%M")
        raw = f"{self.symbol}|{self.side.value}|{stamp}|{self.stop_price:.4f}"
        return "qd-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class Order:
    """A sized, risk-approved order ready for the broker."""
    symbol: str
    side: Side
    quantity: float
    stop_price: float
    target_price: float
    client_order_id: str
    intent: Optional[Intent] = None
    limit_price: Optional[float] = None

    @property
    def is_market(self) -> bool:
        return self.limit_price is None


@dataclass(frozen=True, slots=True)
class Fill:
    symbol: str
    side: Side
    quantity: float
    price: float
    ts: datetime
    order_id: str = ""
    commission: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "ts", ensure_utc(self.ts))

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.price


@dataclass(slots=True)
class Position:
    """An open position. Mutable — it accumulates as fills land."""
    symbol: str
    side: Side
    quantity: float
    entry_price: float
    stop_price: float
    target_price: float
    opened_at: datetime
    intent: Optional[Intent] = None
    sector: str = "unknown"
    partial_taken: bool = False
    stop_moved_to_breakeven: bool = False
    opened_session: Optional[str] = None   # trading-day key, for the PDT rule
    # The stop distance AT ENTRY, frozen. Captured on construction because the
    # live stop moves and R must not move with it.
    entry_risk_per_share: float = 0.0
    # Size at entry, and what partial takes have already banked. `quantity`
    # shrinks as partials fill; these keep the trade's full life recoverable.
    original_quantity: float = 0.0
    realized_pnl: float = 0.0
    realized_r: float = 0.0

    def __post_init__(self) -> None:
        self.opened_at = ensure_utc(self.opened_at)
        if not self.entry_risk_per_share:
            self.entry_risk_per_share = abs(self.entry_price - self.stop_price)
        if not self.original_quantity:
            self.original_quantity = abs(self.quantity)

    @property
    def notional(self) -> float:
        return abs(self.quantity) * self.entry_price

    @property
    def risk_per_share(self) -> float:
        """Distance to the CURRENT stop — a live exposure measure.

        Not the R yardstick; see `initial_risk_per_share`.
        """
        return abs(self.entry_price - self.stop_price)

    @property
    def initial_risk_per_share(self) -> float:
        """The risk taken AT ENTRY. This is what one R means, permanently.

        Measuring R against the live stop looks equivalent and is not. Moving
        the stop to breakeven after a partial take — which this strategy does
        by design — drives the distance to zero, and `r_multiple` then reports
        0.0 for a trade that is winning. In the first corrected evaluation
        that zeroed the R of every trade good enough to bank a partial; and
        because `win_rate` counts `r > 0`, those same winners were then
        counted as losses. Expectancy and win rate were both dragged down by
        exactly the trades that worked.

        Trailing a stop into profit is the mirror failure: it shrinks the
        denominator and inflates R.
        """
        return self.entry_risk_per_share

    @property
    def open_risk(self) -> float:
        """Cash lost if the stop fills at its price. Slippage makes the real
        number worse; this is the planned floor, not a guarantee.

        Deliberately follows the LIVE stop — it answers "what is exposed right
        now", which a breakeven move genuinely does reduce.
        """
        return abs(self.quantity) * self.risk_per_share

    def unrealized(self, mark: float) -> float:
        return (mark - self.entry_price) * self.quantity * self.side.sign

    def r_multiple(self, mark: float) -> float:
        """Progress in units of INITIAL risk — the only comparable P&L scale
        across a $12 stock and a $600 one."""
        rps = self.initial_risk_per_share
        if rps <= 0:
            return 0.0
        return ((mark - self.entry_price) * self.side.sign) / rps


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _clamp(x: float, lo: float, hi: float) -> float:
    if x != x:          # NaN — propagating it would poison every downstream sum
        return 0.0
    return max(lo, min(hi, x))


def clamp(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return _clamp(x, lo, hi)


def squash(x: float, scale: float) -> float:
    """Map an unbounded reading onto [-1, 1] with tanh.

    Used wherever a raw measurement (a z-score, a surprise percentage, a
    premium ratio) needs to become a comparable score. `scale` is the value
    that maps to ~0.76, so it should be set to "clearly significant, not
    extreme". The saturation is the point: a 40-sigma reading is a data error
    far more often than it is a 40-sigma opportunity, and tanh refuses to let
    one broken input dominate the sum.
    """
    if scale <= 0:
        return 0.0
    return math.tanh(x / scale)


def latest_by_key(records: Iterable[Any], key: str = "symbol") -> dict[str, Any]:
    out: dict[str, Any] = {}
    for r in records:
        out[getattr(r, key)] = r
    return out


__all__ = [
    "UTC", "utcnow", "ensure_utc", "clamp", "squash", "latest_by_key", "replace",
    "Side", "Right", "Aggressor", "Source",
    "Bar", "Quote", "NewsItem", "EarningsEvent", "OptionContract", "OptionTrade",
    "Evidence", "Intent", "Order", "Fill", "Position",
]
