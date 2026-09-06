"""
research.screener — surface companies where insiders are buying, with context.

This is the FINDER, and it is a different product from the study described in
`docs/superpowers/specs/2026-09-05-future-leaders-design.md`. The distinction
matters enough to state at the top:

    THE STUDY asks "does insider buying predict returns?", needs price history
    and a benchmark, uses the design's REGISTERED weights, and takes months.

    THE FINDER asks "who is buying their own stock right now?", needs only SEC
    filings, uses a SCREENER weighting that prefers agreement over size, and
    works today.

**A candidate here is not a recommendation.** What the code guarantees is that
the facts are right: this person really is the CEO, they really bought on the
open market with their own money, it really was not a grant or a pre-scheduled
plan. Whether that predicts anything is the study's question, and it is open.

Nothing in this module may be reported as evidence about the hypothesis. The
screener weighting exists to rank candidates for a human to read, and
`InsiderScore.weighting` records which preset produced every number so a
ranking preference can never be mistaken for a measurement.

WHAT IS ATTACHED TO EACH CANDIDATE

  1. Insider buying    Form 4 open-market purchases, clustered and scored.
  2. Institutions      13F holders, with the lag stated — a 13F published in
                       February describes 31 December, and the position may
                       have been closed in January. Confirmation, never a lead.
  3. Financials        Revenue growth and acceleration, margins, net debt,
                       R&D intensity — from XBRL, first-filing only.
  4. Management        Verbatim quotes from the latest earnings press release.
                       Never summarised, never scored.

Every field is optional and absence is reported rather than filled. A candidate
with no financials is a candidate whose financials could not be read, which is
a different fact from a company with no revenue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional, Sequence

from qd.features.insider import InsiderScore, ScoreWeights, score_insiders
from qd.providers.forms import InsiderTransaction
from qd.providers.fundamentals import (
    CompanyProfile,
    EdgarCompany,
    Fundamentals,
    ManagementCommentary,
)
from qd.providers.holdings13f import Holding
from qd.types import ensure_utc

logger = logging.getLogger(__name__)

# A 13F reports quarter-end and is due 45 days later, so a holding can be four
# and a half months old before it is readable. Stated on every candidate.
INSTITUTIONAL_LAG_NOTE = (
    "13F is a quarter-end snapshot published up to 45 days later; a position "
    "shown here may already have been sold"
)


@dataclass(frozen=True, slots=True)
class InstitutionalInterest:
    """Which funds reported holding this name, and how that changed."""
    holders: int = 0
    total_value_usd: float = 0.0
    new_positions: int = 0
    as_of_quarter: Optional[datetime] = None
    lag_note: str = INSTITUTIONAL_LAG_NOTE

    @property
    def available(self) -> bool:
        return self.holders > 0


@dataclass(frozen=True, slots=True)
class Candidate:
    """One company the screener surfaced, with everything known about it."""
    symbol: str
    cik: str
    name: str
    insider: InsiderScore
    profile: Optional[CompanyProfile] = None
    fundamentals: Optional[Fundamentals] = None
    institutions: Optional[InstitutionalInterest] = None
    commentary: Optional[ManagementCommentary] = None
    excluded_reason: str = ""

    @property
    def included(self) -> bool:
        return not self.excluded_reason

    @property
    def missing_context(self) -> tuple[str, ...]:
        """What could not be read — never confused with what is absent.

        A candidate with no financials is one whose financials could not be
        read. That is a different fact from a company with no revenue, and
        collapsing them would let a data gap read as a weak business.
        """
        gaps = []
        if self.fundamentals is None or not self.fundamentals.revenue:
            gaps.append("financials")
        if self.institutions is None or not self.institutions.available:
            gaps.append("institutional holdings")
        if self.commentary is None or not self.commentary.available:
            gaps.append("management commentary")
        return tuple(gaps)

    def summary(self) -> str:
        """One line per candidate, for a terminal listing."""
        s = self.insider
        bits = [f"{self.symbol or self.cik:<8} {s.score:5.1f}",
                f"{s.cluster_size} insider{'s' if s.cluster_size != 1 else ''}",
                s.top_seniority.name.lower(),
                f"${s.purchase_value:,.0f}"]
        f = self.fundamentals
        if f is not None and f.revenue_growth_yoy is not None:
            bits.append(f"rev {f.revenue_growth_yoy:+.0%}")
            if f.revenue_acceleration is not None:
                bits.append(f"accel {f.revenue_acceleration:+.0%}")
        if f is not None and f.gross_margin is not None:
            bits.append(f"gm {f.gross_margin:.0%}")
        if self.institutions and self.institutions.available:
            bits.append(f"{self.institutions.holders} funds")
        return "  ".join(bits)


def build_candidate(
    transactions: Sequence[InsiderTransaction],
    *,
    as_of: datetime,
    company: Optional[EdgarCompany] = None,
    holdings: Optional[Iterable[Holding]] = None,
    prior_holdings: Optional[Iterable[Holding]] = None,
    window_days: int = 120,
    require_operating_company: bool = True,
) -> Candidate:
    """Assemble one candidate from its filings.

    `company` is optional so the insider half can be built and tested with no
    network. When it is absent the candidate simply carries no context, which
    `missing_context` reports rather than hiding.
    """
    now = ensure_utc(as_of)
    cik = transactions[0].issuer_cik if transactions else ""
    symbol = transactions[0].symbol if transactions else ""

    score = score_insiders(transactions, as_of=now, window_days=window_days,
                           weights=ScoreWeights.screener())

    profile = fundamentals = commentary = None
    if company is not None and cik:
        profile = company.profile(cik)
        # Resolve the entity BEFORE spending requests on its financials: a fund
        # share class is excluded regardless of what its numbers say.
        if profile is not None and require_operating_company \
                and not profile.is_operating_company:
            return Candidate(
                symbol=profile.symbol or symbol, cik=cik,
                name=profile.name, insider=score, profile=profile,
                excluded_reason=profile.non_operating_reason,
            )
        fundamentals = company.fundamentals(cik, as_of=now)
        commentary = company.commentary(cik)

    return Candidate(
        symbol=(profile.symbol if profile and profile.symbol else symbol),
        cik=cik,
        name=profile.name if profile else "",
        insider=score,
        profile=profile,
        fundamentals=fundamentals,
        institutions=summarise_institutions(holdings, prior_holdings),
        commentary=commentary,
    )


def summarise_institutions(
    holdings: Optional[Iterable[Holding]],
    prior: Optional[Iterable[Holding]] = None,
) -> Optional[InstitutionalInterest]:
    """Count funds reporting this name, and how many are new.

    Equity positions only — a PRN row is bond principal in the same field as a
    share count, and summing the two produces holdings that look enormous.
    """
    if holdings is None:
        return None
    rows = [h for h in holdings if h.is_equity_position]
    if not rows:
        return InstitutionalInterest()

    new = 0
    if prior is not None:
        held_before = {h.accession for h in prior if h.is_equity_position}
        new = len({h.accession for h in rows} - held_before)

    return InstitutionalInterest(
        holders=len({h.accession for h in rows}),
        total_value_usd=sum(h.value_usd for h in rows),
        new_positions=new,
        as_of_quarter=next((h.period_of_report for h in rows
                            if h.period_of_report is not None), None),
    )


def rank(candidates: Iterable[Candidate], *, limit: int = 25) -> list[Candidate]:
    """Included candidates, strongest insider signal first.

    Excluded names are dropped rather than ranked low, because "this is a fund"
    is not a weak signal — it is not a candidate at all.
    """
    kept = [c for c in candidates if c.included]
    kept.sort(key=lambda c: c.insider.score, reverse=True)
    return kept[:limit]


__all__ = [
    "Candidate", "InstitutionalInterest", "build_candidate",
    "summarise_institutions", "rank", "INSTITUTIONAL_LAG_NOTE",
]
