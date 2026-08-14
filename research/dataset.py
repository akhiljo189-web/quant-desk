"""
research.dataset — build, persist and reload point-in-time historical archives.

The walk-forward run needs real history in a `ReplayDataset`. This module
fetches it, writes it to disk, and reads it back — and the whole file is really
about one problem: **look-ahead can be baked in at load time, where the
point-in-time choke point cannot catch it.**

`ReplayProvider._bound()` guarantees that nothing with `known_at` in the
simulated future is ever served. It cannot guarantee that `known_at` is
*correct*. If the builder stamps a record with a timestamp earlier than the
moment its information really existed, the provider will faithfully serve a
lie, the engine will trade on it, and every downstream test will pass. So the
timestamps are constructed carefully here, the round trip is asserted to
preserve them exactly, and `verify()` refuses archives that violate the
invariants.

Four biases are known, and each is handled explicitly rather than hoped away:

  SURVIVORSHIP     A universe chosen today and backfilled contains only names
                   that still exist. Companies that were delisted, acquired or
                   collapsed are absent, and they are exactly the ones that
                   went badly. This is the largest bias in the archive and the
                   builder cannot fix it — it records the universe's origin in
                   the manifest and warns, so the number is read knowing it is
                   optimistic.

  SPLIT ADJUSTMENT Polygon's adjusted series divides pre-split bars by a ratio
                   determined by a split that had not happened yet. Returns and
                   ATR-percentages are unaffected (ratios survive), but the
                   absolute price band in UniverseConfig is applied to a price
                   nobody could have quoted. Recorded in the manifest; second
                   order for mid-caps, which split rarely, but not zero.

  RESTATEMENT      Earnings actuals are the value reported TODAY, not
                   necessarily the value first printed. Restatements are rare
                   and usually small, but the archive is a snapshot of current
                   belief about the past, and `built_at` records when that
                   snapshot was taken.

  NEWS ARRIVAL     Historical news carries a publication time, not the moment
                   it reached a subscriber. The Polygon adapter adds a
                   conservative fixed latency rather than assuming instant
                   receipt, which would grant the backtest a speed advantage no
                   live run can reproduce.

The archive is JSONL plus a manifest, written per symbol so a multi-year fetch
across a large universe is resumable. That matters more than it sounds: these
builds take hours, hit rate limits, and get interrupted, and a builder that
must restart from zero is a builder that quietly encourages shortcuts.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Iterable, Optional, Sequence

from qd.clock import CALENDAR, MarketCalendar
from qd.providers.replay import ReplayDataset
from qd.types import (
    UTC, Bar, EarningsEvent, NewsItem, OptionContract, OptionTrade, Quote,
    Right, ensure_utc, utcnow,
)

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1


# ─────────────────────────────────────────────────────────────────────────────
# Serialisation
# ─────────────────────────────────────────────────────────────────────────────
#
# Hand-written rather than generic dataclass dumping. Every field that
# determines `known_at` is written explicitly, so that a change to a record
# type breaks the round-trip test loudly instead of silently dropping a
# timestamp and defaulting it to something plausible.

def _iso(dt: Optional[datetime]) -> Optional[str]:
    return ensure_utc(dt).isoformat() if dt is not None else None


def _dt(raw: Optional[str]) -> Optional[datetime]:
    return ensure_utc(datetime.fromisoformat(raw)) if raw else None


def bar_to_dict(b: Bar) -> dict:
    return {
        "symbol": b.symbol, "start": _iso(b.start), "end": _iso(b.end),
        "o": b.open, "h": b.high, "l": b.low, "c": b.close,
        "v": b.volume, "vw": b.vwap, "n": b.trades,
    }


def bar_from_dict(d: dict) -> Bar:
    return Bar(
        symbol=d["symbol"], start=_dt(d["start"]), end=_dt(d["end"]),
        open=d["o"], high=d["h"], low=d["l"], close=d["c"],
        volume=d["v"], vwap=d.get("vw"), trades=d.get("n"),
    )


def news_to_dict(n: NewsItem) -> dict:
    return {
        "id": n.id, "symbols": list(n.symbols), "headline": n.headline,
        "summary": n.summary, "published_at": _iso(n.published_at),
        "received_at": _iso(n.received_at), "source": n.source, "url": n.url,
        "labels": list(n.labels),
    }


def news_from_dict(d: dict) -> NewsItem:
    return NewsItem(
        id=d["id"], symbols=tuple(d["symbols"]), headline=d["headline"],
        summary=d.get("summary", ""), published_at=_dt(d["published_at"]),
        received_at=_dt(d["received_at"]), source=d.get("source", ""),
        url=d.get("url", ""), labels=tuple(d.get("labels", ())),
    )


def earnings_to_dict(e: EarningsEvent) -> dict:
    return {
        "symbol": e.symbol, "report_date": _iso(e.report_date),
        "session": e.session, "scheduled_known_at": _iso(e.scheduled_known_at),
        "eps_estimate": e.eps_estimate, "eps_actual": e.eps_actual,
        "revenue_estimate": e.revenue_estimate, "revenue_actual": e.revenue_actual,
        "released_at": _iso(e.released_at), "fiscal_period": e.fiscal_period,
    }


def earnings_from_dict(d: dict) -> EarningsEvent:
    return EarningsEvent(
        symbol=d["symbol"], report_date=_dt(d["report_date"]),
        session=d["session"], scheduled_known_at=_dt(d["scheduled_known_at"]),
        eps_estimate=d.get("eps_estimate"), eps_actual=d.get("eps_actual"),
        revenue_estimate=d.get("revenue_estimate"),
        revenue_actual=d.get("revenue_actual"),
        released_at=_dt(d.get("released_at")),
        fiscal_period=d.get("fiscal_period", ""),
    )


def option_trade_to_dict(t: OptionTrade) -> dict:
    c = t.contract
    return {
        "underlying": c.underlying, "expiry": _iso(c.expiry), "strike": c.strike,
        "right": c.right.value, "occ": c.occ_symbol,
        "ts": _iso(t.ts), "price": t.price, "size": t.size,
        "exchange": t.exchange, "conditions": list(t.conditions),
        "bid": t.nbbo_bid, "ask": t.nbbo_ask, "spot": t.underlying_price,
        "oi": t.open_interest, "received_at": _iso(t.received_at),
    }


def option_trade_from_dict(d: dict) -> OptionTrade:
    return OptionTrade(
        contract=OptionContract(
            underlying=d["underlying"], expiry=_dt(d["expiry"]),
            strike=d["strike"], right=Right(d["right"]),
            occ_symbol=d.get("occ", ""),
        ),
        ts=_dt(d["ts"]), price=d["price"], size=d["size"],
        exchange=d.get("exchange", ""), conditions=tuple(d.get("conditions", ())),
        nbbo_bid=d.get("bid"), nbbo_ask=d.get("ask"),
        underlying_price=d.get("spot"), open_interest=d.get("oi"),
        received_at=_dt(d.get("received_at")),
    )


_WRITERS = {
    "daily": (bar_to_dict, bar_from_dict),
    "intraday": (bar_to_dict, bar_from_dict),
    "news": (news_to_dict, news_from_dict),
    "earnings": (earnings_to_dict, earnings_from_dict),
    "options": (option_trade_to_dict, option_trade_from_dict),
}


# ─────────────────────────────────────────────────────────────────────────────
# Spec and manifest
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BuildSpec:
    symbols: tuple[str, ...]
    start: date
    end: date
    bar_minutes: int = 5
    include_intraday: bool = True
    include_news: bool = True
    include_earnings: bool = True
    include_options: bool = False    # expensive; flow is confirmation-only
    # Daily history fetched BEFORE `start`, so the regime layer and ATR are
    # warm on day one. Without it the first ~3 months of any run are spent with
    # every symbol UNKNOWN, which silently shortens the test window.
    warmup_days: int = 200
    market_symbol: str = "SPY"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["symbols"] = list(self.symbols)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "BuildSpec":
        d = dict(d)
        d["symbols"] = tuple(d["symbols"])
        d["start"] = date.fromisoformat(d["start"])
        d["end"] = date.fromisoformat(d["end"])
        return cls(**d)

    @property
    def fetch_start(self) -> date:
        return self.start - timedelta(days=self.warmup_days)

    def all_symbols(self) -> tuple[str, ...]:
        """Universe plus the index used for market regime."""
        if self.market_symbol and self.market_symbol not in self.symbols:
            return self.symbols + (self.market_symbol,)
        return self.symbols


@dataclass
class Manifest:
    """Provenance. What was fetched, when, and under which assumptions.

    Written so that a result can be traced back to the exact archive that
    produced it. An unlabelled dataset is a result you cannot defend six months
    later, when you no longer remember whether prices were adjusted or which
    universe snapshot was used.
    """
    schema: int = SCHEMA_VERSION
    built_at: str = ""
    spec: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)
    completed_symbols: list[str] = field(default_factory=list)
    adjusted_prices: bool = True
    schedule_lead_days: int = 21
    news_latency_seconds: float = 30.0
    warnings: list[str] = field(default_factory=list)
    provider_notes: dict = field(default_factory=dict)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as fh:
            json.dump(asdict(self), fh, indent=2, sort_keys=True)

    @classmethod
    def load(cls, path: str) -> Optional["Manifest"]:
        if not os.path.exists(path):
            return None
        with open(path) as fh:
            return cls(**json.load(fh))

    def describe(self) -> str:
        parts = [f"built {self.built_at[:19]}", f"schema v{self.schema}"]
        parts += [f"{k}={v}" for k, v in sorted(self.counts.items())]
        return " | ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Archive layout
# ─────────────────────────────────────────────────────────────────────────────

def _path(root: str, kind: str, symbol: Optional[str] = None) -> str:
    if symbol:
        return os.path.join(root, kind, f"{symbol.upper()}.jsonl")
    return os.path.join(root, f"{kind}.jsonl")


def _write_jsonl(path: str, records: Iterable[Any], to_dict) -> int:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    n = 0
    # Write to a temp file and rename. A build interrupted mid-write otherwise
    # leaves a truncated JSONL that loads as a shorter, entirely plausible
    # history — which is the worst failure mode available, because nothing
    # about it looks wrong.
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        for r in records:
            fh.write(json.dumps(to_dict(r), separators=(",", ":")) + "\n")
            n += 1
    os.replace(tmp, path)
    return n


def _read_jsonl(path: str, from_dict) -> list:
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(from_dict(json.loads(line)))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Builder
# ─────────────────────────────────────────────────────────────────────────────

class DatasetBuilder:
    """Fetches history from live providers into a resumable on-disk archive."""

    def __init__(
        self,
        root: str,
        market=None,
        earnings=None,
        news=None,
        options=None,
        cal: MarketCalendar = CALENDAR,
    ) -> None:
        self.root = root
        self.market = market
        self.earnings = earnings
        self.news = news
        self.options = options
        self.cal = cal

    # ── fetching ─────────────────────────────────────────────────────────────

    def build(self, spec: BuildSpec, resume: bool = True) -> Manifest:
        """Fetch everything in `spec` and write it to the archive.

        Resumable: symbols already present on disk are skipped unless
        `resume=False`. Re-running after an interruption or a rate-limit wall
        costs only the missing symbols.
        """
        os.makedirs(self.root, exist_ok=True)
        manifest_path = os.path.join(self.root, "manifest.json")
        manifest = (Manifest.load(manifest_path) if resume else None) or Manifest()
        manifest.built_at = manifest.built_at or utcnow().isoformat()
        manifest.spec = spec.to_dict()
        done = set(manifest.completed_symbols) if resume else set()

        start = datetime.combine(spec.fetch_start, datetime.min.time(), tzinfo=UTC)
        end = datetime.combine(spec.end, datetime.max.time(), tzinfo=UTC)

        for symbol in spec.all_symbols():
            if symbol in done:
                logger.info("%s: already in archive, skipping", symbol)
                continue
            try:
                self._fetch_symbol(symbol, spec, start, end, manifest)
                manifest.completed_symbols.append(symbol)
                manifest.save(manifest_path)      # checkpoint after each symbol
            except Exception as exc:
                logger.error("%s: fetch failed: %s", symbol, exc)
                manifest.warnings.append(f"{symbol}: fetch failed — {exc}")
                manifest.save(manifest_path)

        # Earnings are fetched once for the whole universe, not per symbol.
        if spec.include_earnings and self.earnings is not None:
            try:
                # Prefer the SUE path when the provider has one. The consensus
                # endpoint returns four quarters, which across this universe
                # cannot fill a walk-forward; the fallback exists for stub
                # providers in tests and for adapters that only carry consensus.
                fetch = getattr(self.earnings, "sue_earnings", None) \
                    or self.earnings.earnings
                events = fetch(list(spec.symbols), start, end)
                n = _write_jsonl(
                    _path(self.root, "earnings"), events, earnings_to_dict
                )
                manifest.counts["earnings"] = n
                logger.info("earnings: %d events", n)
            except Exception as exc:
                logger.error("earnings fetch failed: %s", exc)
                manifest.warnings.append(f"earnings: {exc}")

        self._add_standard_warnings(spec, manifest)
        manifest.save(manifest_path)
        logger.info("archive written to %s — %s", self.root, manifest.describe())
        return manifest

    def _fetch_symbol(
        self, symbol: str, spec: BuildSpec, start: datetime, end: datetime,
        manifest: Manifest,
    ) -> None:
        if self.market is None:
            raise RuntimeError("no market provider configured")

        daily = self.market.daily_bars(symbol, start, end)
        n_daily = _write_jsonl(_path(self.root, "daily", symbol), daily, bar_to_dict)
        manifest.counts["daily"] = manifest.counts.get("daily", 0) + n_daily
        logger.info("%s: %d daily bars", symbol, n_daily)

        if spec.include_intraday and symbol != spec.market_symbol:
            # Intraday only from the decision window, not the warm-up. Warm-up
            # exists to prime daily indicators; fetching minute bars for it
            # multiplies the download for data no decision ever reads.
            intraday_start = datetime.combine(
                spec.start, datetime.min.time(), tzinfo=UTC
            )
            bars = self.market.bars(symbol, intraday_start, end, spec.bar_minutes)
            n = _write_jsonl(_path(self.root, "intraday", symbol), bars, bar_to_dict)
            manifest.counts["intraday"] = manifest.counts.get("intraday", 0) + n
            logger.info("%s: %d intraday bars", symbol, n)

        if spec.include_news and self.news is not None and symbol != spec.market_symbol:
            items = self.news.news([symbol], start, end)
            n = _write_jsonl(_path(self.root, "news", symbol), items, news_to_dict)
            manifest.counts["news"] = manifest.counts.get("news", 0) + n

        if spec.include_options and self.options is not None and symbol != spec.market_symbol:
            trades = self.options.option_trades(symbol, start, end)
            n = _write_jsonl(
                _path(self.root, "options", symbol), trades, option_trade_to_dict
            )
            manifest.counts["options"] = manifest.counts.get("options", 0) + n

    def _add_standard_warnings(self, spec: BuildSpec, manifest: Manifest) -> None:
        """Record the biases that cannot be fixed, so results are read with
        them in view rather than discovered later."""
        existing = set(manifest.warnings)

        def add(w: str) -> None:
            if w not in existing:
                manifest.warnings.append(w)
                existing.add(w)

        add(
            "SURVIVORSHIP: the universe was chosen from names existing at build "
            "time, so delisted, acquired and collapsed companies are absent. "
            "Results are optimistic by an unmeasured amount."
        )
        if manifest.adjusted_prices:
            add(
                "SPLIT-ADJUSTED PRICES: pre-split bars are divided by a ratio "
                "set by a split that had not yet occurred. Returns and ATR%% are "
                "unaffected; the absolute price band in UniverseConfig is "
                "applied to prices nobody could have quoted."
            )
        add(
            f"EARNINGS SCHEDULE: scheduled_known_at assumes "
            f"{manifest.schedule_lead_days}d of lead time; Finnhub does not "
            f"report announcement dates."
        )
        add(
            "RESTATEMENTS: earnings actuals are today's values, not necessarily "
            "the figures first printed."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Save / load
# ─────────────────────────────────────────────────────────────────────────────

def save(dataset: ReplayDataset, root: str, manifest: Optional[Manifest] = None) -> Manifest:
    """Write an in-memory dataset to an archive (used by tests and fixtures)."""
    manifest = manifest or Manifest(built_at=utcnow().isoformat())
    counts: dict[str, int] = {}

    for symbol, bars in dataset.daily.items():
        counts["daily"] = counts.get("daily", 0) + _write_jsonl(
            _path(root, "daily", symbol), bars, bar_to_dict
        )
    for symbol, bars in dataset.bars.items():
        counts["intraday"] = counts.get("intraday", 0) + _write_jsonl(
            _path(root, "intraday", symbol), bars, bar_to_dict
        )
    for symbol, trades in dataset.option_trades.items():
        counts["options"] = counts.get("options", 0) + _write_jsonl(
            _path(root, "options", symbol), trades, option_trade_to_dict
        )
    if dataset.news:
        counts["news"] = _write_jsonl(
            _path(root, "news_all"), dataset.news, news_to_dict
        )
    if dataset.earnings:
        counts["earnings"] = _write_jsonl(
            _path(root, "earnings"), dataset.earnings, earnings_to_dict
        )

    manifest.counts = counts
    manifest.save(os.path.join(root, "manifest.json"))
    return manifest


def load(root: str) -> tuple[ReplayDataset, Optional[Manifest]]:
    """Read an archive back into a ReplayDataset.

    The round trip must preserve `known_at` exactly — see
    `tests/test_dataset.py`. Anything that perturbs a timestamp here shifts the
    entire simulation's information boundary, and does so invisibly.
    """
    ds = ReplayDataset()

    for kind, adder in (("daily", ds.add_daily), ("intraday", ds.add_bars)):
        d = os.path.join(root, kind)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".jsonl"):
                continue
            symbol = fn[:-6]
            adder(symbol, _read_jsonl(os.path.join(d, fn), bar_from_dict))

    opts = os.path.join(root, "options")
    if os.path.isdir(opts):
        for fn in sorted(os.listdir(opts)):
            if fn.endswith(".jsonl"):
                ds.add_option_trades(
                    fn[:-6],
                    _read_jsonl(os.path.join(opts, fn), option_trade_from_dict),
                )

    news_dir = os.path.join(root, "news")
    if os.path.isdir(news_dir):
        for fn in sorted(os.listdir(news_dir)):
            if fn.endswith(".jsonl"):
                ds.add_news(_read_jsonl(os.path.join(news_dir, fn), news_from_dict))
    ds.add_news(_read_jsonl(_path(root, "news_all"), news_from_dict))
    ds.add_earnings(_read_jsonl(_path(root, "earnings"), earnings_from_dict))

    ds.freeze()
    return ds, Manifest.load(os.path.join(root, "manifest.json"))


# ─────────────────────────────────────────────────────────────────────────────
# Verification
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VerifyReport:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    def explain(self) -> str:
        lines = [f"dataset verification: {'PASS' if self.ok else 'FAIL'}"]
        for k, v in sorted(self.stats.items()):
            lines.append(f"  {k}: {v}")
        for e in self.errors:
            lines.append(f"  ERROR   {e}")
        for w in self.warnings:
            lines.append(f"  warning {w}")
        return "\n".join(lines)


def verify(
    dataset: ReplayDataset,
    min_daily_bars: int = 60,
    cal: MarketCalendar = CALENDAR,
) -> VerifyReport:
    """Data-quality gate. Run before trusting any result from an archive.

    Errors are conditions that would corrupt a backtest silently. Warnings are
    conditions that make one hard to interpret. The distinction matters: a
    corrupt archive produces a confident wrong number, which is worse than an
    archive that produces an obviously incomplete one.
    """
    errors: list[str] = []
    warnings: list[str] = []
    stats: dict[str, Any] = {}

    stats["symbols"] = len(set(dataset.daily) | set(dataset.bars))
    stats["daily_bars"] = sum(len(v) for v in dataset.daily.values())
    stats["intraday_bars"] = sum(len(v) for v in dataset.bars.values())
    stats["news"] = len(dataset.news)
    stats["earnings"] = len(dataset.earnings)
    stats["option_trades"] = sum(len(v) for v in dataset.option_trades.values())

    # A record knowable before it happened is a corrupt archive, full stop.
    for symbol, bars in list(dataset.daily.items()) + list(dataset.bars.items()):
        for b in bars:
            if b.known_at < b.event_time:
                errors.append(f"{symbol}: bar known_at {b.known_at} precedes start {b.start}")
                break
        ordered = all(bars[i].end <= bars[i + 1].end for i in range(len(bars) - 1))
        if not ordered:
            errors.append(f"{symbol}: bars are not ordered by close time")

    for n in dataset.news:
        if n.received_at < n.published_at - timedelta(seconds=1):
            errors.append(f"news {n.id}: received before published")
            break

    # The earnings leak, checked directly: actuals must never be knowable at
    # the moment the schedule is.
    for e in dataset.earnings:
        if e.eps_actual is not None and e.released_at is None:
            errors.append(f"{e.symbol}: has actual EPS but no released_at — the "
                          f"PEAD gate cannot hide it")
        if e.released_at is not None and e.released_at < e.scheduled_known_at:
            errors.append(f"{e.symbol}: released_at precedes scheduled_known_at")
        if e.has_actuals_at(e.scheduled_known_at):
            errors.append(f"{e.symbol}: actuals readable at schedule time — LEAK")

    # Coverage.
    thin = [s for s, bars in dataset.daily.items() if len(bars) < min_daily_bars]
    if thin:
        warnings.append(
            f"{len(thin)} symbol(s) have under {min_daily_bars} daily bars, so the "
            f"regime layer will report UNKNOWN and block them: {', '.join(thin[:6])}"
            + (" ..." if len(thin) > 6 else "")
        )

    no_intraday = [s for s in dataset.daily if s not in dataset.bars]
    if no_intraday:
        warnings.append(
            f"{len(no_intraday)} symbol(s) have daily but no intraday bars: "
            f"{', '.join(no_intraday[:6])}" + (" ..." if len(no_intraday) > 6 else "")
        )

    if not dataset.earnings:
        warnings.append(
            "no earnings events — the PEAD trigger cannot fire and the run will "
            "produce zero trades by construction"
        )

    symbols_with_earnings = {e.symbol for e in dataset.earnings}
    missing = sorted(set(dataset.daily) - symbols_with_earnings - {"SPY"})
    if missing:
        warnings.append(
            f"{len(missing)} symbol(s) have no earnings rows: "
            f"{', '.join(missing[:6])}" + (" ..." if len(missing) > 6 else "")
        )

    return VerifyReport(ok=not errors, errors=errors, warnings=warnings, stats=stats)


__all__ = [
    "BuildSpec", "Manifest", "DatasetBuilder", "VerifyReport",
    "save", "load", "verify", "SCHEMA_VERSION",
    "bar_to_dict", "bar_from_dict", "news_to_dict", "news_from_dict",
    "earnings_to_dict", "earnings_from_dict",
    "option_trade_to_dict", "option_trade_from_dict",
]
