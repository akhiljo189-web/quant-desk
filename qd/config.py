"""
qd.config — every tunable in one place, loaded from the environment once.

Configuration is a dataclass rather than module-level globals so that replay can
construct a variant (different costs, different thresholds) without mutating
process state and quietly changing the behaviour of a concurrently running
backtest. `Settings.load()` reads the environment; nothing else in the codebase
touches os.environ.

Numbers here are *starting points chosen to be defensible*, not fitted values.
Anything tuned against a backtest until it looked good would be a curve fit, and
the whole point of the gate in qd/gate.py is that no number in this file is
trusted until walk-forward evidence supports it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import timedelta
from enum import Enum
from typing import Mapping, Optional

from qd.types import Source


class Mode(str, Enum):
    REPLAY = "replay"   # no network, no broker, deterministic
    PAPER = "paper"     # live data, simulated money at the broker
    LIVE = "live"       # real money — gated, see qd/gate.py

    @property
    def is_money(self) -> bool:
        return self is Mode.LIVE


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_f(key: str, default: float) -> float:
    raw = _env(key)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_i(key: str, default: int) -> int:
    raw = _env(key)
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


def _env_b(key: str, default: bool) -> bool:
    raw = _env(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


# ─────────────────────────────────────────────────────────────────────────────
# Risk
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RiskConfig:
    """The risk envelope. These limits bind unconditionally — no signal, however
    strong, is allowed to argue its way past one.

    Sizing runs risk-first: position size falls out of the stop distance and the
    per-trade cash risk, never out of a notional target. The exposure caps can
    only ever *reduce* that size.
    """

    # Per-trade risk as a percent of equity. 0.5% means twenty consecutive
    # full-stop losses to lose 10% — survivable, and slow enough that the
    # circuit breakers fire long before ruin.
    risk_pct: float = 0.50

    # Hard floor on stop distance as a multiple of ATR. Tighter stops buy more
    # size, which is exactly why they are tempting and why they get shaken out
    # by ordinary intraday noise before the thesis has a chance to play out.
    min_stop_atr_mult: float = 1.0
    default_stop_atr_mult: float = 1.5
    max_stop_atr_mult: float = 3.0

    # A stop closer than this to entry is noise, not structure, whatever ATR says.
    min_stop_pct: float = 0.005      # 0.5% of price

    # Exposure ceilings, as multiples of equity.
    max_gross_exposure: float = 1.50   # sum of |notional|
    max_net_exposure: float = 1.00     # |longs - shorts|, directional bet size
    max_position_notional_pct: float = 20.0   # single name, % of equity
    max_sector_notional_pct: float = 35.0     # one sector, % of equity

    # Concurrency.
    max_open_positions: int = 8
    max_positions_per_sector: int = 3
    max_new_positions_per_day: int = 5

    # Total open risk: the sum of every position's distance-to-stop. This is the
    # number that matters in a gap-down — position count and notional both
    # understate what a correlated overnight move actually costs.
    max_total_open_risk_pct: float = 2.0

    # Circuit breakers. Realised losses only; unrealised swings are noise.
    daily_loss_stop_pct: float = 2.0
    weekly_loss_stop_pct: float = 4.0
    daily_loss_streak: int = 4
    weekly_loss_streak: int = 8

    # Overnight risk. Gaps ignore stops entirely — a stop is an order resting
    # against a market that is not trading, and it fills at the open print,
    # wherever that lands. Holding through earnings is the extreme case.
    allow_overnight: bool = True
    max_overnight_positions: int = 4
    flatten_before_earnings: bool = True

    # Pattern Day Trader rule: under $25k equity, a US margin account gets 3 day
    # trades per rolling 5 business days. Breaching it freezes the account for
    # 90 days, which ends the experiment regardless of P&L.
    pdt_equity_threshold: float = 25_000.0
    pdt_max_day_trades: int = 3
    pdt_enforce: bool = True

    # Data staleness. If the feeds stop updating, the system is trading against
    # a frozen picture of the market — the one state where doing nothing is
    # strictly better than acting on what it believes it knows.
    max_quote_age: timedelta = timedelta(seconds=30)
    max_bar_age: timedelta = timedelta(minutes=5)
    staleness_halts_entries: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Universe / liquidity
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class UniverseConfig:
    """What is allowed to be traded at all.

    Liquidity filters do more work than any signal. Illiquid names show the
    prettiest backtests — huge percentage moves, apparently clean trends — and
    are untradeable at size, because the spread and impact eat the whole edge.
    """
    symbols: tuple[str, ...] = (
        "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AMD",
        "AVGO", "NFLX", "CRM", "ORCL", "QCOM", "MU", "INTC", "PLTR",
        "JPM", "BAC", "XOM", "CVX", "UNH", "LLY", "COST", "WMT",
    )
    min_price: float = 5.00          # sub-$5 is a different market with different rules
    max_price: float = 2_000.00
    min_avg_dollar_volume: float = 20_000_000.0   # 20-day ADV in dollars
    max_spread_bps: float = 15.0     # at decision time, not on average
    min_atr_pct: float = 0.8         # too quiet: the target is inside the noise
    max_atr_pct: float = 12.0        # too wild: stops are unaffordably wide

    # Rough sector map for correlation caps. Static and approximate on purpose:
    # a wrong-but-stable grouping still stops eight semiconductor names being
    # loaded as if they were eight independent bets.
    sectors: Mapping[str, str] = field(default_factory=lambda: {
        "AAPL": "tech_hw", "MSFT": "tech_sw", "NVDA": "semis", "AMZN": "consumer_disc",
        "META": "tech_sw", "GOOGL": "tech_sw", "TSLA": "consumer_disc", "AMD": "semis",
        "AVGO": "semis", "NFLX": "tech_sw", "CRM": "tech_sw", "ORCL": "tech_sw",
        "QCOM": "semis", "MU": "semis", "INTC": "semis", "PLTR": "tech_sw",
        "JPM": "financials", "BAC": "financials", "XOM": "energy", "CVX": "energy",
        "UNH": "healthcare", "LLY": "healthcare", "COST": "staples", "WMT": "staples",
    })

    def sector_of(self, symbol: str) -> str:
        return self.sectors.get(symbol.upper(), "unknown")


# ─────────────────────────────────────────────────────────────────────────────
# Evidence channels
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MarketConfig:
    atr_period: int = 14
    rvol_lookback_days: int = 20
    trend_fast: int = 20
    trend_slow: int = 50
    bar_minutes: int = 5
    min_bars_for_signal: int = 30
    rvol_significant: float = 2.0     # maps to a ~0.76 score through squash()
    gap_significant_pct: float = 2.0
    vwap_dist_significant_atr: float = 1.0
    ttl: timedelta = timedelta(minutes=45)


@dataclass(frozen=True)
class NewsConfig:
    # Half-life, not a cliff. An hour after a headline the surprise is mostly
    # priced; the score should reflect that continuously.
    half_life: timedelta = timedelta(minutes=20)
    ttl: timedelta = timedelta(hours=2)

    # Novelty window: a near-identical headline inside this window is a rewrite
    # of a story we already scored, and rewrites arrive in bursts. Without this
    # the tenth aggregator repeat reads as ten independent confirmations.
    novelty_window: timedelta = timedelta(hours=6)
    repeat_confidence_decay: float = 0.35   # each repeat multiplies confidence

    # Feed latency beyond which a headline is assumed already priced.
    max_latency: timedelta = timedelta(minutes=5)

    # Source tiering. A primary wire is not a stock-promotion blog.
    source_weights: Mapping[str, float] = field(default_factory=lambda: {
        "benzinga": 0.85, "reuters": 1.0, "bloomberg": 1.0, "dow jones": 1.0,
        "business wire": 0.95, "globe newswire": 0.9, "pr newswire": 0.9,
        "sec": 1.0, "cnbc": 0.7, "barrons": 0.7, "seeking alpha": 0.4,
        "motley fool": 0.2, "zacks": 0.3, "": 0.5,
    })
    default_source_weight: float = 0.5
    min_confidence: float = 0.25


@dataclass(frozen=True)
class EarningsConfig:
    # Do not hold into a print. The distribution is bimodal and the stop does
    # not exist across the gap — this is a coin flip with the position size of
    # a considered trade.
    blackout_before: timedelta = timedelta(hours=24)
    blackout_after: timedelta = timedelta(minutes=30)   # let the auction settle

    # Post-earnings announcement drift: the most durable published anomaly in
    # equities, and also one that has decayed steadily as it became famous.
    # Traded here as evidence, never as a standalone reason.
    pead_window: timedelta = timedelta(days=3)
    surprise_significant: float = 0.10     # 10% EPS surprise
    revenue_significant: float = 0.03      # 3% revenue surprise
    min_confidence: float = 0.3
    ttl: timedelta = timedelta(days=3)


@dataclass(frozen=True)
class OptionsFlowConfig:
    """Thresholds for reading the options tape.

    The defaults are deliberately strict. Loose thresholds turn ordinary
    market-maker hedging and retail lottery-ticket volume into a stream of
    "institutional accumulation" alerts, and the tape is mostly that.
    """
    window: timedelta = timedelta(minutes=30)
    ttl: timedelta = timedelta(minutes=60)

    # What counts as notable size.
    min_trade_premium: float = 25_000.0     # ignore odd-lot retail noise
    block_size: int = 250                   # contracts in one print
    block_premium: float = 100_000.0

    # Sweep: one order shredded across venues to fill immediately. Urgency is
    # the signal — a patient buyer works one exchange and waits for a better fill.
    sweep_window: timedelta = timedelta(milliseconds=500)
    sweep_min_legs: int = 3
    sweep_min_exchanges: int = 2
    sweep_min_premium: float = 50_000.0

    # Multi-leg detection. Near-simultaneous prints on the same underlying with
    # opposing deltas are almost always one spread, and a spread is a far
    # smaller directional bet than its call leg alone implies. Counting the legs
    # separately is the single most common way flow readers overstate conviction.
    spread_window: timedelta = timedelta(milliseconds=250)
    spread_size_tolerance: float = 0.2      # leg sizes within 20% => paired
    spread_confidence_penalty: float = 0.4

    # Contract relevance.
    max_dte: float = 90.0        # further out is positioning, not a near-term view
    min_dte: float = 0.5         # 0DTE is dominated by gamma scalping, not direction
    max_abs_moneyness: float = 0.15   # >15% OTM is lottery volume

    # Unusualness is relative. $2M of premium is a rounding error in AAPL and a
    # regime change in a mid-cap, so scoring is against the symbol's own baseline.
    baseline_days: int = 20
    zscore_significant: float = 2.5
    min_premium_for_signal: float = 250_000.0
    min_confidence: float = 0.3

    # Opening interest confirmation. Size above prior OI means new risk;
    # below it may just be someone closing.
    require_opening_likely: bool = False
    opening_confidence_bonus: float = 0.15


# ─────────────────────────────────────────────────────────────────────────────
# Strategy
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ContextConfig:
    """Which regimes the strategy is allowed to trade in.

    Declared, not discovered. A strategy that trades every regime is claiming
    to work in all of them, which is a claim almost nothing survives — and one
    that a blended backtest average will happily appear to support.

    The measurement thresholds live in qd/context.py and are deliberately not
    exposed here: a regime filter tuned against returns is a second strategy
    hiding inside the filter, and when the system loses money you cannot tell
    which of the two failed.
    """
    enabled: bool = True
    # PEAD is an event-driven drift, not a trend-following signal. It is
    # allowed in chop because the drift does not require the broader tape to
    # trend — it requires the market to reprice one name slowly.
    allowed_regimes: tuple = ("trend_up", "trend_down", "chop")
    # The index is a different matter: a long single-name position in a
    # market-wide downtrend is fighting the factor that explains most of its
    # return.
    allowed_market_regimes: Optional[tuple] = ("trend_up", "chop")
    market_symbol: str = "SPY"
    require_known_regime: bool = True


@dataclass(frozen=True)
class StrategyConfig:
    """How the four channels combine into a decision.

    The central rule is confluence: independent channels must agree. One loud
    reading is usually a data artefact — a busted print, a duplicated headline,
    a mislabelled spread leg. Two channels being wrong in the same direction at
    the same moment is far rarer than either being wrong alone.
    """
    min_sources: int = 2
    min_conviction: float = 0.35
    min_reward_risk: float = 1.5

    # Per-channel weights. Market structure is weighted highest not because it
    # predicts best but because it is the least corruptible input: a bar is a
    # bar, whereas news and flow both require interpretation before they mean
    # anything.
    weights: Mapping[Source, float] = field(default_factory=lambda: {
        Source.MARKET: 1.00,
        Source.NEWS: 0.85,
        Source.EARNINGS: 0.70,
        Source.OPTIONS_FLOW: 0.80,
    })

    # A channel arguing the other way does more damage than one agreeing does
    # good. Disagreement means the picture is genuinely unclear, and unclear is
    # a reason to stand aside rather than to size down.
    conflict_penalty: float = 1.5
    veto_on_conflict_above: float = 0.5    # opposing weight this strong blocks entry

    # Targets and exits, in units of initial risk.
    target_r: float = 2.0
    partial_take_r: float = 1.0
    partial_fraction: float = 0.5
    breakeven_after_partial: bool = True
    trail_atr_mult: float = 2.0
    max_hold: timedelta = timedelta(hours=30)
    time_stop_r_threshold: float = 0.3     # cut trades going nowhere

    # Session gating. The opening auction is a different market: spreads are
    # wide, the first prints are unreliable, and overnight orders unwind into
    # them. Waiting a few minutes costs little and avoids a lot.
    no_entry_first_minutes: float = 5.0
    no_entry_last_minutes: float = 20.0
    allow_extended_hours: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Execution
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ExecutionConfig:
    """Order handling and the cost model used in research.

    Costs are modelled pessimistically. Commission-free equities are not free —
    the spread is the fee, and the fill is on the wrong side of the mid by
    construction. Every strategy that dies at realistic costs looked profitable
    at optimistic ones.
    """
    use_bracket_orders: bool = True     # stop lives at the broker, not in this process
    limit_offset_bps: float = 5.0       # marketable limit, never a naked market order
    max_slippage_bps: float = 25.0      # abandon the entry beyond this
    order_timeout: timedelta = timedelta(seconds=30)

    commission_per_share: float = 0.0
    commission_min: float = 0.0
    sec_fee_rate: float = 0.0000278     # sells only, per dollar
    finra_taf_per_share: float = 0.000166

    # Research cost model: half the spread each way, plus impact. The multiplier
    # is swept in evaluation — a result that only survives at 1.0x is not a result.
    spread_cost_mult: float = 1.0
    slippage_bps: float = 2.0
    cost_stress_mults: tuple[float, ...] = (1.0, 1.5, 2.0)

    reconcile_on_start: bool = True
    cancel_orphan_orders: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Providers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProviderConfig:
    polygon_api_key: str = ""
    polygon_base: str = "https://api.polygon.io"
    alpaca_key_id: str = ""
    alpaca_secret: str = ""
    alpaca_paper_base: str = "https://paper-api.alpaca.markets"
    alpaca_live_base: str = "https://api.alpaca.markets"
    request_timeout: float = 10.0
    max_retries: int = 3
    rate_limit_per_min: int = 100
    cache_dir: str = "data/cache"
    record_responses: bool = True   # every response archived for honest replay

    def alpaca_base(self, mode: Mode) -> str:
        return self.alpaca_live_base if mode is Mode.LIVE else self.alpaca_paper_base


# ─────────────────────────────────────────────────────────────────────────────
# Root
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Settings:
    mode: Mode = Mode.PAPER
    risk: RiskConfig = field(default_factory=RiskConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    market: MarketConfig = field(default_factory=MarketConfig)
    news: NewsConfig = field(default_factory=NewsConfig)
    earnings: EarningsConfig = field(default_factory=EarningsConfig)
    options: OptionsFlowConfig = field(default_factory=OptionsFlowConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    providers: ProviderConfig = field(default_factory=ProviderConfig)

    poll_interval: timedelta = timedelta(seconds=20)
    journal_path: str = "data/journal.jsonl"
    proof_path: str = "data/edge_proof.json"
    log_path: str = "logs/qd.log"

    @classmethod
    def load(cls, mode: Optional[Mode] = None) -> "Settings":
        """Read the environment. The only place in the codebase that does."""
        raw = _env("QD_MODE", "paper").lower()
        resolved = mode or (Mode(raw) if raw in {m.value for m in Mode} else Mode.PAPER)

        universe = UniverseConfig()
        syms = _env("QD_SYMBOLS")
        if syms:
            universe = replace(
                universe,
                symbols=tuple(s.strip().upper() for s in syms.split(",") if s.strip()),
            )

        return cls(
            mode=resolved,
            risk=replace(
                RiskConfig(),
                risk_pct=_env_f("QD_RISK_PCT", RiskConfig.risk_pct),
                max_open_positions=_env_i("QD_MAX_POSITIONS", RiskConfig.max_open_positions),
                daily_loss_stop_pct=_env_f("QD_DAILY_STOP_PCT", RiskConfig.daily_loss_stop_pct),
                weekly_loss_stop_pct=_env_f("QD_WEEKLY_STOP_PCT", RiskConfig.weekly_loss_stop_pct),
                allow_overnight=_env_b("QD_ALLOW_OVERNIGHT", RiskConfig.allow_overnight),
                pdt_enforce=_env_b("QD_PDT_ENFORCE", RiskConfig.pdt_enforce),
            ),
            universe=universe,
            strategy=replace(
                StrategyConfig(),
                min_sources=_env_i("QD_MIN_SOURCES", StrategyConfig.min_sources),
                min_conviction=_env_f("QD_MIN_CONVICTION", StrategyConfig.min_conviction),
            ),
            providers=replace(
                ProviderConfig(),
                polygon_api_key=_env("POLYGON_API_KEY"),
                alpaca_key_id=_env("ALPACA_KEY_ID"),
                alpaca_secret=_env("ALPACA_SECRET_KEY"),
                cache_dir=_env("QD_CACHE_DIR", ProviderConfig.cache_dir),
            ),
            journal_path=_env("QD_JOURNAL", "data/journal.jsonl"),
            proof_path=_env("QD_PROOF", "data/edge_proof.json"),
        )

    def describe(self) -> str:
        r = self.risk
        return (
            f"mode={self.mode.value} symbols={len(self.universe.symbols)} "
            f"risk={r.risk_pct}%/trade max_pos={r.max_open_positions} "
            f"gross<={r.max_gross_exposure}x daily_stop={r.daily_loss_stop_pct}% "
            f"min_sources={self.strategy.min_sources} "
            f"min_conviction={self.strategy.min_conviction}"
        )


__all__ = [
    "Mode", "Settings", "RiskConfig", "UniverseConfig", "ContextConfig",
    "MarketConfig", "NewsConfig", "EarningsConfig", "OptionsFlowConfig",
    "StrategyConfig", "ExecutionConfig", "ProviderConfig",
]
