"""
qd.providers.edgar — exact earnings-announcement timestamps from SEC filings.

This is the point-in-time backbone of the PEAD trigger, and it is better than
anything we could buy.

Finnhub's free tier gives the surprise (estimate, actual) but dates it by
FISCAL QUARTER END. A company whose quarter ends 30 June announces in late
July. Treating the period end as the event date would start the drift trade
four weeks before the earnings existed — no error, entirely plausible output,
and a spectacular fake backtest. That is the exact leak class this codebase is
built to prevent, so the announcement instant has to come from somewhere
trustworthy.

EDGAR is the source of record, and it is unusually clean for this purpose:

  ITEM 2.02   "Results of Operations and Financial Condition" — the SEC's own
              code for an earnings release. Identifying which 8-K announced
              earnings is therefore a LABEL LOOKUP, not a heuristic. Filings
              are tagged by the filer under a legal obligation.

  acceptanceDateTime
              The instant EDGAR accepted the document and it became public,
              to the second. Not a convention, not an assumption — the moment
              the information entered the world.

Together these replace two guesses previously flagged as the weakest links in
the system: `SCHEDULE_LEAD = 21 days` and the invented 07:00/16:15 ET release
convention. A paid calendar feed would give a vendor's transcription of the
same event; this is the event.

What EDGAR cannot do: look forward. A filing exists only once filed, so there
is no scheduled-earnings calendar here. Backtests are unaffected — every
filing in history is in the past — but the live "do not hold into a print"
blackout loses its primary source and falls back to the quarterly cadence
estimate in `estimate_next_report`. That degradation is explicit rather than
silent; see `qd/features/earnings.py`.

SEC access rules, which are conditions of use rather than suggestions:
  - a User-Agent naming the requester, or requests are refused
  - at most 10 requests/second
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping, Optional, Sequence

from qd.config import ProviderConfig
from qd.providers.base import ProviderError
from qd.providers.http import HttpClient, RateLimiter, Recorder
from qd.types import UTC, ensure_utc

logger = logging.getLogger(__name__)

SEC_DATA = "https://data.sec.gov"
SEC_WWW = "https://www.sec.gov"

# "Results of Operations and Financial Condition" — the earnings release item.
EARNINGS_ITEM = "2.02"

# Periodic reports, used as a fallback when a company reports results inside
# the 10-Q/10-K itself without a separate 8-K. Less precise: the periodic
# report is sometimes filed days after the press release.
PERIODIC_FORMS = ("10-Q", "10-K")


# Process-wide cache. The ticker->CIK mapping is static enough that refetching
# it per component is pure waste, and enough waste to trip SEC's rate limiter.
_CIK_MAP: Optional[dict[str, str]] = None


def _cik_cache_path(cache_dir: str) -> str:
    import os
    return os.path.join(cache_dir, "sec_cik_map.json")


def _load_cik_cache(cache_dir: str) -> Optional[dict[str, str]]:
    import os
    path = _cik_cache_path(cache_dir)
    if not os.path.exists(path):
        return None
    # A month-old mapping is fine: CIKs are permanent and new listings are
    # rare. Refetching beyond that picks up recent IPOs and ticker changes.
    age = datetime.now().timestamp() - os.path.getmtime(path)
    if age > 30 * 86400:
        return None
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception:
        return None


def _save_cik_cache(cache_dir: str, mapping: dict[str, str]) -> None:
    import os
    try:
        os.makedirs(cache_dir, exist_ok=True)
        tmp = _cik_cache_path(cache_dir) + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(mapping, fh)
        os.replace(tmp, _cik_cache_path(cache_dir))
    except Exception as exc:
        logger.debug("could not cache CIK map: %s", exc)


@dataclass(frozen=True)
class Filing:
    """One SEC filing, with the instant it became public."""
    symbol: str
    cik: str
    form: str
    filed_date: datetime          # the filing DATE (no time component)
    accepted_at: datetime         # the exact acceptance instant — what we use
    items: tuple[str, ...] = ()
    accession: str = ""
    primary_doc: str = ""

    @property
    def is_earnings_release(self) -> bool:
        return self.form == "8-K" and EARNINGS_ITEM in self.items

    @property
    def is_periodic(self) -> bool:
        return self.form in PERIODIC_FORMS

    def url(self) -> str:
        if not self.accession:
            return ""
        acc = self.accession.replace("-", "")
        return f"{SEC_WWW}/Archives/edgar/data/{int(self.cik)}/{acc}/{self.primary_doc}"


class EdgarFilings:
    """SEC EDGAR submissions reader."""

    def __init__(
        self,
        cfg: ProviderConfig,
        user_agent: Optional[str] = None,
    ) -> None:
        ua = user_agent or cfg.sec_user_agent
        if not ua or "@" not in ua:
            # The SEC requires a contact address. Failing loudly here beats a
            # wall of 403s that look like a network problem.
            raise ProviderError(
                "SEC requires a User-Agent containing a contact email — set "
                "SEC_USER_AGENT, e.g. 'quant-desk research you@example.com'"
            )
        self.cfg = cfg
        self.user_agent = ua
        self._cik_map: Optional[dict[str, str]] = None

        headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
        # SEC allows 10 req/s; 300/min leaves headroom and still fetches a
        # 40-name universe in well under a minute.
        self._data = HttpClient(
            base_url=SEC_DATA, headers=headers, timeout=cfg.request_timeout,
            max_retries=cfg.max_retries, rate_limiter=RateLimiter(300),
            recorder=Recorder(cfg.cache_dir, cfg.record_responses),
        )
        self._www = HttpClient(
            base_url=SEC_WWW, headers=headers, timeout=cfg.request_timeout,
            max_retries=cfg.max_retries, rate_limiter=RateLimiter(300),
            recorder=Recorder(cfg.cache_dir, cfg.record_responses),
        )

    # ── ticker → CIK ─────────────────────────────────────────────────────────

    def cik_map(self) -> dict[str, str]:
        """Ticker → zero-padded 10-digit CIK.

        Cached at MODULE level, not per instance. The mapping is ~10k rows,
        essentially static, and several components build their own
        EdgarFilings — so a per-instance cache means the same large file is
        pulled once per component per run. That is what tripped SEC's rate
        limiter during validation, and at a 35-name universe it would have
        been far worse. Also persisted to disk so repeat runs skip it entirely.
        """
        global _CIK_MAP
        if _CIK_MAP is not None:
            return _CIK_MAP
        if self._cik_map is not None:
            return self._cik_map

        cached = _load_cik_cache(self.cfg.cache_dir)
        if cached:
            _CIK_MAP = cached
            return cached

        payload = self._www.get("/files/company_tickers.json", tag="cik-map")
        out: dict[str, str] = {}
        for row in (payload or {}).values():
            ticker = str(row.get("ticker", "")).upper()
            cik = row.get("cik_str")
            if ticker and cik is not None:
                out[ticker] = f"{int(cik):010d}"
        if out:
            _CIK_MAP = out
            _save_cik_cache(self.cfg.cache_dir, out)
            logger.info("edgar: %d ticker->CIK mappings (cached)", len(out))
        self._cik_map = out
        return out

    def cik_for(self, symbol: str) -> Optional[str]:
        return self.cik_map().get(symbol.upper())

    # ── filings ──────────────────────────────────────────────────────────────

    def filings(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        forms: Sequence[str] = ("8-K", "10-Q", "10-K"),
    ) -> list[Filing]:
        """Filings for a symbol, newest first in EDGAR's order.

        Reads `filings.recent`, which holds roughly the last thousand filings —
        many years for a typical mid-cap. Companies filing more heavily than
        that have older filings in separate files; `recent_only_warning` reports
        when the window may not reach `start`, so a short history is visible
        rather than silently becoming "this company had no earnings".
        """
        cik = self.cik_for(symbol)
        if cik is None:
            logger.warning("edgar: no CIK for %s", symbol)
            return []

        payload = self._data.get(f"/submissions/CIK{cik}.json", tag=f"edgar-{symbol}")
        if not payload:
            return []

        recent = (payload.get("filings") or {}).get("recent") or {}
        n = len(recent.get("form", []))
        if n == 0:
            return []

        want = set(forms)
        out: list[Filing] = []
        for i in range(n):
            form = recent["form"][i]
            if form not in want:
                continue
            accepted = _parse_accepted(recent.get("acceptanceDateTime", [None] * n)[i])
            filed = _parse_date(recent.get("filingDate", [None] * n)[i])
            if accepted is None or filed is None:
                continue
            if start and accepted < ensure_utc(start):
                continue
            if end and accepted > ensure_utc(end):
                continue
            raw_items = recent.get("items", [""] * n)[i] or ""
            out.append(Filing(
                symbol=symbol.upper(), cik=cik, form=form,
                filed_date=filed, accepted_at=accepted,
                items=tuple(s.strip() for s in raw_items.split(",") if s.strip()),
                accession=recent.get("accessionNumber", [""] * n)[i] or "",
                primary_doc=recent.get("primaryDocument", [""] * n)[i] or "",
            ))

        out.sort(key=lambda f: f.accepted_at)
        return out

    def earnings_releases(
        self,
        symbol: str,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        include_periodic: bool = True,
    ) -> list[Filing]:
        """8-K filings tagged item 2.02 — the earnings announcements.

        `include_periodic` keeps 10-Q/10-K filings as a fallback for companies
        that report results inside the periodic report rather than issuing a
        separate 8-K. They are less precise (the periodic report can lag the
        press release by days) so they are only used when no 2.02 exists for
        the period; the join in `finnhub_edgar.py` prefers 2.02 always.
        """
        forms = ("8-K",) + (PERIODIC_FORMS if include_periodic else ())
        got = self.filings(symbol, start, end, forms)
        return [f for f in got if f.is_earnings_release or (include_periodic and f.is_periodic)]

    def recent_only_warning(self, symbol: str, start: datetime) -> Optional[str]:
        """Whether `filings.recent` may not reach back to `start`.

        A truncated window looks downstream like a company that simply did not
        report, which is the kind of missing data that quietly shortens a
        backtest instead of announcing itself.
        """
        got = self.filings(symbol, forms=("8-K", "10-Q", "10-K"))
        if not got:
            return f"{symbol}: no filings returned"
        oldest = got[0].accepted_at
        if oldest > ensure_utc(start):
            return (
                f"{symbol}: EDGAR recent window starts {oldest:%Y-%m-%d}, after the "
                f"requested {ensure_utc(start):%Y-%m-%d} — earlier earnings are missing"
            )
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _parse_accepted(raw: Optional[str]) -> Optional[datetime]:
    """Parse EDGAR's acceptanceDateTime, e.g. '2026-07-30T20:30:28.000Z'.

    Always UTC. This value is the whole point of the module, so a parse failure
    returns None and drops the filing rather than substituting a date — a
    midnight fallback would place an after-close release sixteen hours early.
    """
    if not raw:
        return None
    try:
        return ensure_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def estimate_next_report(
    history: Sequence[Filing], now: datetime, lookahead: timedelta = timedelta(days=120)
) -> Optional[datetime]:
    """Estimate the next earnings date from the company's own cadence.

    EDGAR cannot look forward, so this stands in for the scheduled-earnings
    calendar the live blackout would otherwise use. Companies report on a
    strikingly regular quarterly rhythm, so "same point in the cycle, one
    quarter on" is usually right to within a few days.

    It is an ESTIMATE and callers must treat it as one: widen the blackout
    around it rather than trusting the day. Being early costs a skipped trade;
    being late means holding through a print, which is the failure the blackout
    exists to prevent.
    """
    now = ensure_utc(now)
    releases = sorted(
        (f for f in history if f.is_earnings_release), key=lambda f: f.accepted_at
    )
    if len(releases) < 2:
        return None

    gaps = [
        (releases[i].accepted_at - releases[i - 1].accepted_at).days
        for i in range(1, len(releases))
    ]
    recent = [g for g in gaps[-4:] if 60 <= g <= 120]     # sane quarterly gaps only
    if not recent:
        return None

    cadence = sum(recent) / len(recent)
    nxt = releases[-1].accepted_at + timedelta(days=cadence)
    while nxt < now:
        nxt += timedelta(days=cadence)
    return nxt if nxt - now <= lookahead else None


__all__ = [
    "EdgarFilings", "Filing", "estimate_next_report",
    "EARNINGS_ITEM", "PERIODIC_FORMS",
]
