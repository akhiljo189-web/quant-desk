"""
research.evaluate — turn replay results into a verdict, and into the edge proof.

Produces the artefact `qd/gate.py` demands before any real money moves. The
standard is deliberately hostile, because the thing being guarded against is not
a bad strategy — it is a plausible one that happens to be noise.

What gets measured, and why each one is here:

  COST STRESS       Expectancy at 1x, 1.5x and 2x the modelled cost. Anything
                    that survives only at 1x is an artefact of the cost model.
                    Real costs are never the friendly assumption, and the gap
                    between assumed and realised cost is where most paper edges
                    quietly live.

  ORDERING BAND     Every result recomputed assuming stops fill first, then
                    targets first. The truth is inside that band. If the band
                    straddles zero, there is no measured edge — only an
                    assumption doing the work.

  FOLD CONSISTENCY  Expectancy per consecutive period. An edge concentrated in
                    one fold is a description of that period, and the next
                    period will be a different one.

  SAMPLE SIZE       With 50 trades, a +0.3R mean is comfortably explicable by
                    chance. The t-statistic here is a rough guide, not a
                    hypothesis test — trade returns are fat-tailed and serially
                    correlated, which violates what the t-distribution assumes.
                    Treat it as "obviously too few" versus "possibly enough".

The evaluator will happily conclude NO EDGE, and that is a successful run. A
research tool that cannot return a negative verdict is not measuring anything.
"""

from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional, Sequence

from qd.config import Settings
from qd.gate import EdgeProof, FoldResult, Requirements, DEFAULT_REQUIREMENTS
from qd.providers.replay import ReplayDataset
from qd.types import utcnow
from research.replay import ReplayResult, run, walk_forward

logger = logging.getLogger(__name__)


@dataclass
class CostSweep:
    results: dict[float, ReplayResult] = field(default_factory=dict)

    def expectancies(self) -> dict[str, float]:
        return {str(k): v.expectancy_r() for k, v in sorted(self.results.items())}

    def survives(self, mult: float) -> bool:
        r = self.results.get(mult)
        return r is not None and r.expectancy_r() > 0

    def table(self) -> str:
        lines = ["  cost   trades  expectancy      PF    maxDD"]
        for mult, r in sorted(self.results.items()):
            lines.append(
                f"  {mult:>4.1f}x  {r.count:>6d}  {r.expectancy_r():>+9.4f}R  "
                f"{r.profit_factor():>6.3f}  {r.max_drawdown_pct():>6.1f}%"
            )
        return "\n".join(lines)


@dataclass
class Evaluation:
    base: ReplayResult
    sweep: CostSweep
    folds: list[ReplayResult]
    ordering_band: tuple[float, float]
    verdict: str
    reasons: list[str] = field(default_factory=list)

    @property
    def has_edge(self) -> bool:
        return self.verdict == "EDGE"

    def t_stat(self) -> float:
        """Rough significance of mean R against zero.

        Understates uncertainty — trade outcomes are neither independent nor
        normal — so read it as a floor on how uncertain the result is, never as
        a p-value.
        """
        rs = [t.r_multiple for t in self.base.trades]
        if len(rs) < 2:
            return 0.0
        sd = statistics.pstdev(rs)
        if sd <= 0:
            return 0.0
        return (statistics.fmean(rs) / sd) * math.sqrt(len(rs))

    def report(self) -> str:
        b = self.base
        lines = [
            "=" * 68,
            f"  EVALUATION: {self.verdict}",
            "=" * 68,
            "",
            f"  Period          {b.start:%Y-%m-%d} to {b.end:%Y-%m-%d}",
            f"  Trades          {b.count}",
            f"  Expectancy      {b.expectancy_r():+.4f}R",
            f"  Profit factor   {b.profit_factor():.3f}",
            f"  Win rate        {b.win_rate():.1%}",
            f"  Max drawdown    {b.max_drawdown_pct():.1f}%",
            f"  t-statistic     {self.t_stat():.2f}  (rough; see docstring)",
            "",
            "  Cost stress",
            self.sweep.table(),
            "",
            f"  Ordering band   {self.ordering_band[0]:+.4f}R (stops first) to "
            f"{self.ordering_band[1]:+.4f}R (targets first)",
            f"  Ambiguous bars  {b.ambiguity_ratio():.1%} of trades",
            "",
            "  Walk-forward folds",
        ]
        for i, f in enumerate(self.folds, 1):
            lines.append(
                f"    fold {i}  {f.start:%Y-%m-%d}..{f.end:%Y-%m-%d}  "
                f"n={f.count:<5d} {f.expectancy_r():+.4f}R  PF={f.profit_factor():.3f}"
            )
        if b.blocked:
            lines += ["", "  Most common reasons for NOT trading"]
            for reason, n in sorted(b.blocked.items(), key=lambda kv: -kv[1])[:8]:
                lines.append(f"    {n:>6d}  {reason}")
        if self.reasons:
            lines += ["", "  Verdict reasoning"]
            lines += [f"    - {r}" for r in self.reasons]
        lines.append("=" * 68)
        return "\n".join(lines)

    def to_proof(self, version: str = "0.1.0", notes: str = "") -> EdgeProof:
        proof = EdgeProof(
            generated_at=utcnow(),
            strategy_version=version,
            data_start=self.base.start.strftime("%Y-%m-%d") if self.base.start else "",
            data_end=self.base.end.strftime("%Y-%m-%d") if self.base.end else "",
            oos_trades=sum(f.count for f in self.folds),
            expectancy_r=self.base.expectancy_r(),
            profit_factor=self.base.profit_factor(),
            max_drawdown_pct=self.base.max_drawdown_pct(),
            cost_mult=self.base.cost_mult,
            stressed=self.sweep.expectancies(),
            folds=tuple(
                FoldResult(
                    label=f"{f.start:%Y-%m-%d}..{f.end:%Y-%m-%d}",
                    trades=f.count,
                    expectancy_r=f.expectancy_r(),
                    profit_factor=f.profit_factor(),
                    max_drawdown_pct=f.max_drawdown_pct(),
                )
                for f in self.folds
            ),
            notes=notes,
        )
        import dataclasses
        return dataclasses.replace(proof, content_hash=proof.compute_hash())


def evaluate(
    settings: Settings,
    dataset: ReplayDataset,
    start: datetime,
    end: datetime,
    equity: float = 100_000.0,
    folds: int = 4,
    req: Requirements = DEFAULT_REQUIREMENTS,
) -> Evaluation:
    """Full evaluation: cost sweep, ordering band, walk-forward, verdict."""
    logger.info("evaluating %s .. %s", start.date(), end.date())

    sweep = CostSweep()
    for mult in settings.execution.cost_stress_mults:
        sweep.results[mult] = run(
            settings, dataset, start, end, equity=equity, cost_mult=mult,
            ordering="worst", journal_path=f"data/replay_cost{mult}.jsonl",
        )
        logger.info("  cost %.1fx: %s", mult, sweep.results[mult].summary())

    base = sweep.results.get(1.0) or next(iter(sweep.results.values()))

    optimistic = run(
        settings, dataset, start, end, equity=equity,
        cost_mult=base.cost_mult, ordering="optimistic",
        journal_path="data/replay_optimistic.jsonl",
    )
    band = (base.expectancy_r(), optimistic.expectancy_r())

    fold_results = walk_forward(
        settings, dataset, start, end, folds=folds, equity=equity,
        cost_mult=req.stress_cost_mult, ordering="worst",
        journal_path="data/replay_folds.jsonl",
    )

    verdict, reasons = _judge(base, sweep, fold_results, band, req)
    return Evaluation(base, sweep, fold_results, band, verdict, reasons)


def _judge(
    base: ReplayResult,
    sweep: CostSweep,
    folds: Sequence[ReplayResult],
    band: tuple[float, float],
    req: Requirements,
) -> tuple[str, list[str]]:
    """Apply the standard. Returns EDGE / NO EDGE / INSUFFICIENT DATA."""
    reasons: list[str] = []

    if base.count < req.min_oos_trades:
        reasons.append(
            f"{base.count} trades is below the {req.min_oos_trades} needed to "
            f"distinguish signal from noise"
        )
        return "INSUFFICIENT DATA", reasons

    fatal = False

    if base.expectancy_r() <= 0:
        reasons.append(f"expectancy {base.expectancy_r():+.4f}R is not positive")
        fatal = True

    if not sweep.survives(req.stress_cost_mult):
        got = sweep.results.get(req.stress_cost_mult)
        val = f"{got.expectancy_r():+.4f}R" if got else "not measured"
        reasons.append(
            f"expectancy at {req.stress_cost_mult}x costs is {val} — "
            f"the edge is inside the spread"
        )
        fatal = True

    # The band straddling zero means the assumption, not the data, produced the
    # sign of the result.
    if band[0] <= 0 <= band[1]:
        reasons.append(
            f"ordering band {band[0]:+.4f}R..{band[1]:+.4f}R spans zero — the "
            f"result depends on assuming which of stop or target filled first"
        )
        fatal = True

    positive = sum(1 for f in folds if f.expectancy_r() > 0)
    ratio = positive / len(folds) if folds else 0.0
    if ratio < req.min_positive_fold_ratio:
        reasons.append(
            f"only {positive}/{len(folds)} folds positive — the result lives in "
            f"specific periods rather than in the signal"
        )
        fatal = True

    if base.profit_factor() < req.min_profit_factor:
        reasons.append(
            f"profit factor {base.profit_factor():.3f} below {req.min_profit_factor}"
        )
        fatal = True

    if base.ambiguity_ratio() > 0.35:
        reasons.append(
            f"{base.ambiguity_ratio():.0%} of trades resolved by the ordering "
            f"assumption — the bar data is too coarse to measure this strategy"
        )
        fatal = True

    if fatal:
        return "NO EDGE", reasons

    reasons.append(
        f"positive at {req.stress_cost_mult}x costs, {positive}/{len(folds)} "
        f"folds positive, ordering band clear of zero"
    )
    return "EDGE", reasons


__all__ = ["evaluate", "Evaluation", "CostSweep"]
