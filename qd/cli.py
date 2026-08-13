"""
qd.cli — command line entry points.

    python -m qd.cli selftest      config sanity + a smoke replay, no network
    python -m qd.cli gate          why live trading is or is not permitted
    python -m qd.cli fetch         build a point-in-time archive from real data
    python -m qd.cli verify        check an archive's integrity
    python -m qd.cli replay        backtest over synthetic or recorded data
    python -m qd.cli evaluate      full evaluation, writes the edge proof
    python -m qd.cli run           the trading loop (paper by default)
    python -m qd.cli journal       what the system did, and what it declined
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta

from qd.clock import CALENDAR, LiveClock
from qd.config import Mode, Settings
from qd.gate import EdgeProof, PaperRecord, check_settings
from qd.journal import Journal
from qd.portfolio import Portfolio
from qd.providers.base import Providers
from qd.risk import validate_config
from qd.types import UTC, utcnow


def _log(verbose: bool = False) -> None:
    os.makedirs("logs", exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout),
                  logging.FileHandler("logs/qd.log")],
    )


# ─────────────────────────────────────────────────────────────────────────────

def cmd_selftest(args) -> int:
    """Everything that can be verified without a network or a key."""
    _log(args.verbose)
    print("quant-desk selftest")
    print("=" * 60)

    s = Settings.load(Mode.REPLAY)
    print(f"config: {s.describe()}")

    problems = validate_config(s.risk)
    if problems:
        print("\nRISK CONFIG PROBLEMS:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("risk config: coherent")

    today = date.today()
    print(f"calendar:   {len(CALENDAR.holidays(today.year))} holidays in "
          f"{today.year}, phase now = {CALENDAR.phase(utcnow()).value}")

    print("\nrunning a smoke replay on synthetic data ...")
    import dataclasses
    from research import replay
    from research.synthetic import SyntheticSpec, generate

    s = dataclasses.replace(
        s, universe=dataclasses.replace(s.universe, symbols=("AAPL", "MSFT"))
    )
    # Generate a long history but replay only the tail. The regime layer needs
    # 60+ closed daily bars before it will classify anything, so a dataset that
    # starts when the replay starts leaves every symbol permanently UNKNOWN and
    # the context gate masks every other refusal. This mirrors real operation:
    # months of daily history behind a short decision window.
    ds = generate(SyntheticSpec(
        symbols=("AAPL", "MSFT", "SPY"), start=date(2025, 9, 1), end=date(2026, 3, 20),
        seed=5, earnings_every_days=45,
    ))
    print(f"  dataset: {ds.summary()}")
    r = replay.run(
        s, ds, datetime(2026, 3, 2, 14, 30, tzinfo=UTC),
        datetime(2026, 3, 20, 20, 0, tzinfo=UTC),
        journal_path="data/selftest.jsonl",
    )
    print(f"  result:  {r.summary()}")
    if r.blocked:
        print("  most common reasons for not trading:")
        for reason, n in sorted(r.blocked.items(), key=lambda kv: -kv[1])[:5]:
            print(f"    {n:>6d}  {reason}")

    print("\nselftest OK")
    print("\nNote: synthetic data has no structure by construction. Trades here")
    print("would indicate a leak, not a discovery.")
    return 0


def cmd_gate(args) -> int:
    _log(args.verbose)
    s = Settings.load(Mode.LIVE if args.live else None)
    proof = EdgeProof.load(s.proof_path)

    print(f"mode:  {s.mode.value}")
    print(f"proof: {s.proof_path}")
    if proof is None:
        print("       (none found)")
    else:
        print(f"       generated {proof.generated_at:%Y-%m-%d} "
              f"({proof.age().days}d ago), intact={proof.is_intact()}")
        print(f"       {proof.oos_trades} OOS trades, "
              f"expectancy {proof.expectancy_r():+.4f}R, PF {proof.profit_factor():.3f}")
        print(f"       stressed: {proof.stressed}")
    print()
    result = check_settings(s, PaperRecord())
    print(result.explain())
    return 0 if result.allowed else 1


def _dataset_for(args, s):
    """Load a real archive if one was given, otherwise synthesise.

    Returns (dataset, settings, start, end). When an archive is used its own
    span bounds the run, so a mistyped date cannot silently replay a window the
    archive does not cover — which would look like a quiet period rather than
    missing data.
    """
    import dataclasses

    if getattr(args, "archive", None):
        from research.dataset import load, verify

        ds, manifest = load(args.archive)
        report = verify(ds)
        if not report.ok:
            print(report.explain())
            raise SystemExit("archive failed verification — refusing to replay it")
        symbols = tuple(sorted(set(ds.daily) - {s.context.market_symbol}))
        if args.symbols:
            wanted = {x.strip().upper() for x in args.symbols.split(",") if x.strip()}
            symbols = tuple(x for x in symbols if x in wanted)
        s = dataclasses.replace(
            s, universe=dataclasses.replace(s.universe, symbols=symbols)
        )
        span = ds.span()
        start = datetime.fromisoformat(args.start).replace(tzinfo=UTC) if args.start else span[0]
        end = datetime.fromisoformat(args.end).replace(tzinfo=UTC) if args.end else span[1]
        if span:
            start, end = max(start, span[0]), min(end, span[1])
        print(f"archive: {args.archive}")
        if manifest:
            print(f"  {manifest.describe()}")
        print(f"  {ds.summary()}")
        print(f"  window: {start:%Y-%m-%d} .. {end:%Y-%m-%d}, {len(symbols)} symbols")
        for w in report.warnings:
            print(f"  warning: {w}")
        return ds, s, start, end

    from research.synthetic import SyntheticSpec, generate

    symbols = tuple(x.strip().upper() for x in args.symbols.split(",") if x.strip())
    s = dataclasses.replace(s, universe=dataclasses.replace(s.universe, symbols=symbols))
    start = datetime.fromisoformat(args.start or "2026-03-02").replace(tzinfo=UTC)
    end = datetime.fromisoformat(args.end or "2026-03-27").replace(tzinfo=UTC)
    print(f"generating synthetic data for {symbols} ...")
    ds = generate(SyntheticSpec(
        symbols=symbols, start=start.date(), end=end.date(), seed=args.seed,
    ))
    print(f"  {ds.summary()}")
    return ds, s, start, end


def cmd_replay(args) -> int:
    _log(args.verbose)
    from research import replay

    s = Settings.load(Mode.REPLAY)
    ds, s, start, end = _dataset_for(args, s)

    r = replay.run(
        s, ds, start, end, equity=args.equity, cost_mult=args.cost,
        ordering=args.ordering,
    )
    print(f"\n{r.summary()}")
    if r.blocked:
        print("\nreasons for not trading:")
        for reason, n in sorted(r.blocked.items(), key=lambda kv: -kv[1])[:10]:
            print(f"  {n:>6d}  {reason}")
    return 0


def cmd_evaluate(args) -> int:
    _log(args.verbose)
    from research.evaluate import evaluate

    s = Settings.load(Mode.REPLAY)
    ds, s, start, end = _dataset_for(args, s)
    print()

    ev = evaluate(s, ds, start, end, equity=args.equity, folds=args.folds)
    print(ev.report())

    if ev.has_edge and args.write_proof:
        ev.to_proof().save(s.proof_path)
        print(f"\nedge proof written to {s.proof_path}")
    elif args.write_proof:
        print("\nno proof written — the verdict was not EDGE")
    return 0


def cmd_run(args) -> int:
    _log(args.verbose)
    s = Settings.load(Mode.LIVE if args.live else Mode.PAPER)

    problems = validate_config(s.risk)
    if problems:
        print("refusing to start — risk config problems:")
        for p in problems:
            print(f"  - {p}")
        return 1

    if s.mode is Mode.LIVE:
        result = check_settings(s, PaperRecord())
        if not result.allowed:
            print(result.explain())
            print("\nrefusing to start in live mode.")
            return 1

    from qd.engine import Engine
    from qd.providers.alpaca import AlpacaBroker
    from qd.providers.finnhub import FinnhubEarnings
    from qd.providers.polygon import PolygonProvider

    try:
        market = PolygonProvider(s.providers)
        broker = AlpacaBroker(s)
        # The PEAD trigger's data source. Without it the strategy has no
        # trigger channel at all and would refuse every candidate, so this is
        # a hard dependency rather than an optional enrichment.
        earnings = FinnhubEarnings(s.providers)
    except Exception as exc:
        print(f"could not reach providers: {exc}")
        print("set POLYGON_API_KEY, FINNHUB_API_KEY, ALPACA_KEY_ID and "
              "ALPACA_SECRET_KEY (see .env.example)")
        return 1

    account = broker.account()
    print(f"account {account.masked_id()}: equity ${account.equity:,.2f}")

    portfolio = Portfolio(account.equity, s.risk, s.universe)
    journal = Journal(s.journal_path)
    providers = Providers(
        market=market, broker=broker, news=market,
        earnings=earnings, options=market,
    )
    engine = Engine(s, providers, portfolio, journal, LiveClock())
    engine.startup()

    interval = s.poll_interval.total_seconds()
    print(f"running ({s.mode.value}); ctrl-c to stop")
    try:
        while True:
            report = engine.cycle()
            print(report.line())
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nstopping")
        journal.event("shutdown")
    return 0


def cmd_fetch(args) -> int:
    """Build a point-in-time historical archive from the live providers."""
    _log(args.verbose)
    from datetime import date as _date

    from qd.providers.finnhub import FinnhubEarnings
    from qd.providers.polygon import PolygonProvider
    from research.dataset import BuildSpec, DatasetBuilder, load, verify

    s = Settings.load(Mode.REPLAY)
    symbols = (
        tuple(x.strip().upper() for x in args.symbols.split(",") if x.strip())
        if args.symbols else s.universe.symbols
    )

    try:
        market = PolygonProvider(s.providers)
        earnings = FinnhubEarnings(s.providers)
    except Exception as exc:
        print(f"could not reach providers: {exc}")
        print("set POLYGON_API_KEY and FINNHUB_API_KEY (see .env.example)")
        return 1

    spec = BuildSpec(
        symbols=symbols,
        start=_date.fromisoformat(args.start),
        end=_date.fromisoformat(args.end),
        bar_minutes=s.market.bar_minutes,
        include_options=args.options,
        warmup_days=args.warmup,
        market_symbol=s.context.market_symbol,
    )

    print(f"fetching {len(spec.all_symbols())} symbols, "
          f"{spec.fetch_start} .. {spec.end} (warm-up {spec.warmup_days}d)")
    print(f"archive: {args.out}")
    if args.options:
        print("including the options tape — this is the expensive one")

    builder = DatasetBuilder(
        args.out, market=market, earnings=earnings, news=market,
        options=market if args.options else None,
    )
    manifest = builder.build(spec, resume=not args.no_resume)

    print(f"\n{manifest.describe()}")
    if manifest.warnings:
        print("\nbiases and caveats recorded in the manifest:")
        for w in manifest.warnings:
            print(f"  - {w}")

    ds, _ = load(args.out)
    report = verify(ds)
    print()
    print(report.explain())
    return 0 if report.ok else 1


def cmd_verify(args) -> int:
    """Check an existing archive without refetching."""
    _log(args.verbose)
    from research.dataset import load, verify

    ds, manifest = load(args.path)
    if manifest is not None:
        print(f"manifest: {manifest.describe()}")
        if manifest.warnings:
            print("caveats:")
            for w in manifest.warnings:
                print(f"  - {w}")
        print()
    report = verify(ds)
    print(report.explain())
    return 0 if report.ok else 1


def cmd_journal(args) -> int:
    s = Settings.load()
    j = Journal(args.path or s.journal_path)

    counts = j.summary()
    if not counts:
        print(f"no journal at {j.path}")
        return 1

    print(f"journal: {j.path}")
    for kind, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {n:>7d}  {kind}")

    blocked = j.blocked_reasons()
    if blocked:
        print("\nwhy trades did NOT happen:")
        for reason, n in list(blocked.items())[:12]:
            print(f"  {n:>7d}  {reason}")

    orders = list(j.read(["order"]))
    if orders:
        print(f"\nlast {min(10, len(orders))} orders:")
        for o in orders[-10:]:
            print(f"  {o['ts'][:19]}  {o['symbol']:6s} {o['side']:4s} "
                  f"qty={o['quantity']:<8g} conv={o.get('conviction', 0):.3f} "
                  f"sources={','.join(o.get('sources', []))}")
    return 0


# ─────────────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="qd", description="quant-desk")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("selftest", help="offline checks + smoke replay")
    sp.set_defaults(func=cmd_selftest)

    sp = sub.add_parser("gate", help="live-trading gate status")
    sp.add_argument("--live", action="store_true", help="evaluate as if mode=live")
    sp.set_defaults(func=cmd_gate)

    sp = sub.add_parser("replay", help="backtest")
    sp.add_argument("--archive", default=None,
                    help="replay a real archive built by `fetch` (else synthetic)")
    sp.add_argument("--symbols", default="AAPL,MSFT,NVDA")
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.add_argument("--equity", type=float, default=100_000.0)
    sp.add_argument("--cost", type=float, default=1.0)
    sp.add_argument("--ordering", default="worst",
                    choices=["worst", "optimistic", "neutral"])
    sp.add_argument("--seed", type=int, default=7)
    sp.set_defaults(func=cmd_replay)

    sp = sub.add_parser("evaluate", help="full evaluation and edge proof")
    sp.add_argument("--archive", default=None,
                    help="evaluate a real archive built by `fetch` (else synthetic)")
    sp.add_argument("--symbols", default="AAPL,MSFT,NVDA")
    sp.add_argument("--start", default=None)
    sp.add_argument("--end", default=None)
    sp.add_argument("--equity", type=float, default=100_000.0)
    sp.add_argument("--folds", type=int, default=4)
    sp.add_argument("--seed", type=int, default=7)
    sp.add_argument("--write-proof", action="store_true")
    sp.set_defaults(func=cmd_evaluate)

    sp = sub.add_parser("run", help="trading loop")
    sp.add_argument("--live", action="store_true", help="real money (gated)")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("fetch", help="build a historical archive from live providers")
    sp.add_argument("--symbols", default=None, help="defaults to the configured universe")
    sp.add_argument("--start", required=True, help="first decision day, YYYY-MM-DD")
    sp.add_argument("--end", required=True, help="last decision day, YYYY-MM-DD")
    sp.add_argument("--out", default="data/archive")
    sp.add_argument("--warmup", type=int, default=200,
                    help="extra daily history before --start, to prime the regime layer")
    sp.add_argument("--options", action="store_true",
                    help="also fetch the options tape (expensive)")
    sp.add_argument("--no-resume", action="store_true",
                    help="refetch symbols already in the archive")
    sp.set_defaults(func=cmd_fetch)

    sp = sub.add_parser("verify", help="check an archive's integrity")
    sp.add_argument("--path", default="data/archive")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("journal", help="inspect the decision journal")
    sp.add_argument("--path", default=None)
    sp.set_defaults(func=cmd_journal)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
