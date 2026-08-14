"""
research.screen — build the universe from a screen, at a point in time.

`UniverseConfig.symbols` is a hand-maintained snapshot and says so. A
hand-maintained list is the single easiest way to fabricate a backtest,
because it is assembled from names that still exist and still trade well
TODAY. Everything that was acquired, delisted, or fell out of the liquidity
band is silently absent, and the absence flatters every result.

So the screen here runs AS OF a date and uses only what was knowable then:

  liquidity   from grouped daily bars on the days before `as_of`. That
              endpoint returns whatever traded that session, so names that
              later delisted are present — which is exactly the point.

  market cap  shares outstanding from SEC XBRL frames (as filed, dated), times
              the UNADJUSTED close on `as_of`. Not today's cap for a company
              that has since tripled.

              Unadjusted matters more than it looks. Grouped bars default to
              split-adjusted prices — restated onto today's basis — while the
              share count is as filed at the time. Multiplying the two put
              O'Reilly in the mid-cap band at $46 a share, because it split
              15:1 three years after the selection date. Booking Holdings, a
              $100B company, came out at $4B. Both are perfectly plausible
              rows in a list of "mid-caps".

  listing     common stock only, from the point-in-time reference listing.
              An oil ETF has a CIK, files with the SEC, and reports units
              outstanding, so units times price yields a "market cap" and it
              screens in as a mid-cap industrial.

WHAT THIS STILL DOES NOT FIX. Polygon's history begins where the data plan
begins, and companies that delisted before the archive starts are invisible
here too. This narrows survivorship; it does not eliminate it. The remaining
bias points the same direction as all the others — flattering — so treat a
marginal result as negative.

THE SELECTION RULE IS FIXED IN ADVANCE, and both halves of it matter. Names are
stratified by market cap, because ranking purely by liquidity piles the universe
into the top of the band and destroys the one out-of-sample structural
prediction the hypothesis makes — that drift weakens monotonically with
capitalisation, which needs spread across the band to be testable at all. And
within a stratum the LOWEST-TURNOVER name is taken, because selecting on volume
selects for prices being argued over continuously, which is the opposite of the
slow repricing the whole hypothesis rests on. See `stratified` for what the
alternative produced.
"""

from __future__ import annotations

import collections
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping, Optional, Sequence

from qd.providers.base import ProviderError
from qd.types import UTC

logger = logging.getLogger(__name__)

# Grouped-bar sessions used for the liquidity estimate, and how many a name
# must actually trade in. A name that traded four days out of twenty-five is
# not a $10M-a-day name, it is a name with one busy week.
ADV_SESSIONS = 25
MIN_SESSIONS = 15

# SEC frames are quarterly instants. Looking back a year covers filers whose
# fiscal calendar puts their disclosure in an awkward quarter, without
# reaching so far back that the share count is stale.
FRAME_QUARTERS_BACK = 5

# Daily dollar volume as a fraction of market cap. A name turning over a tenth
# of itself every day is a speculative vehicle whose price is being contested
# continuously — the opposite of the setup this hypothesis describes, where the
# counterparty is flow that reprices over days. The cap is motivated by the
# hypothesis and fixed before any backtest, not tuned against results.
MAX_TURNOVER = 0.05


@dataclass(frozen=True)
class Candidate:
    symbol: str
    cik: str
    name: str
    price: float
    adv: float
    shares: float
    market_cap: float
    shares_dated: date

    @property
    def turnover(self) -> float:
        return self.adv / self.market_cap if self.market_cap else 0.0

    def line(self) -> str:
        return (f"{self.symbol:<6} ${self.market_cap/1e9:6.2f}B  "
                f"ADV ${self.adv/1e6:7.1f}M  {self.turnover*100:4.1f}%/d  "
                f"${self.price:8.2f}  {self.name[:32]}")


def _sessions_before(as_of: date, count: int) -> list[date]:
    """Calendar days walked backwards; non-sessions simply return no rows."""
    out, d = [], as_of
    while len(out) < count * 2 and (as_of - d).days < count * 3:
        out.append(d)
        d -= timedelta(days=1)
    return out


def dollar_volume(
    polygon, as_of: date, sessions: int = ADV_SESSIONS,
    min_sessions: int = MIN_SESSIONS,
) -> dict[str, tuple[float, float]]:
    """symbol -> (average daily dollar volume, unadjusted close at `as_of`).

    One call per session covers the entire market, including names that later
    delisted. Fetching per-symbol instead would mean starting from a list of
    symbols, which is where survivorship gets in.

    UNADJUSTED, because the close is about to be multiplied by an as-filed
    share count. Dollar volume is unaffected either way — a split multiplies
    volume and divides price by the same factor — so nothing is lost.
    """
    vols: dict[str, list[float]] = collections.defaultdict(list)
    closes: dict[str, float] = {}
    got = 0

    for d in _sessions_before(as_of, sessions):
        if got >= sessions:
            break
        try:
            payload = polygon.http.get(
                f"/v2/aggs/grouped/locale/us/market/stocks/{d.isoformat()}",
                {"adjusted": "false"}, tag=f"grouped-{d.isoformat()}",
            )
        except ProviderError as exc:
            logger.warning("grouped bars %s failed: %s", d, exc)
            continue
        rows = (payload or {}).get("results") or []
        if not rows:
            continue                      # weekend or holiday
        got += 1
        for r in rows:
            sym, v = r.get("T"), r.get("v") or 0.0
            px = r.get("vw") or r.get("c") or 0.0
            if not sym or not px:
                continue
            vols[sym].append(v * px)
            closes.setdefault(sym, r.get("c") or px)

    logger.info("screen: %d sessions, %d symbols traded", got, len(vols))
    return {
        sym: (sum(v) / len(v), closes[sym])
        for sym, v in vols.items() if len(v) >= min_sessions
    }


def common_stock(polygon, as_of: date, max_pages: int = 12) -> set[str]:
    """Symbols listed as COMMON STOCK on `as_of`.

    Without this the screen picks up ETFs, closed-end funds and commodity
    trusts: they carry CIKs, file with the SEC, and report units outstanding,
    so units times price produces a number that looks exactly like a market
    cap. The first run of this screen selected an oil fund and two ProShares
    volatility products as mid-caps.
    """
    out: set[str] = set()
    params = {"market": "stocks", "type": "CS", "date": as_of.isoformat(),
              "limit": 1000}
    for _ in range(max_pages):
        try:
            payload = polygon.http.get("/v3/reference/tickers", dict(params),
                                       tag="tickers-cs")
        except ProviderError as exc:
            logger.warning("ticker listing failed: %s", exc)
            break
        for r in (payload or {}).get("results") or []:
            if r.get("ticker"):
                out.add(r["ticker"].upper())
        nxt = (payload or {}).get("next_url")
        if not nxt:
            break
        cursor = nxt.split("cursor=", 1)[-1].split("&", 1)[0]
        if not cursor:
            break
        params = {"cursor": cursor, "limit": 1000}
    logger.info("screen: %d common stocks listed on %s", len(out), as_of)
    return out


def _frames(as_of: date, back: int = FRAME_QUARTERS_BACK) -> list[str]:
    """Quarterly frame identifiers ending at `as_of`, most recent first."""
    y, q = as_of.year, (as_of.month - 1) // 3 + 1
    out = []
    for _ in range(back):
        out.append(f"CY{y}Q{q}I")
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def shares_outstanding(facts, as_of: date, back: int = FRAME_QUARTERS_BACK
                       ) -> dict[str, tuple[float, date, str]]:
    """CIK -> (shares, the date reported, entity name), latest at `as_of`.

    One SEC call per quarter returns every filer, which is what makes a
    whole-market screen affordable. Values dated after `as_of` are discarded:
    a share count filed next month was not available for the selection.
    """
    out: dict[str, tuple[float, date, str]] = {}
    for frame in _frames(as_of, back):
        try:
            payload = facts.http.get(
                f"/api/xbrl/frames/dei/EntityCommonStockSharesOutstanding/"
                f"shares/{frame}.json", tag=f"frame-{frame}",
            )
        except ProviderError as exc:
            logger.warning("frame %s failed: %s", frame, exc)
            continue
        for row in (payload or {}).get("data") or []:
            try:
                cik = str(int(row["cik"])).zfill(10)
                val = float(row["val"])
                reported = date.fromisoformat(row["end"])
            except (KeyError, ValueError, TypeError):
                continue
            if reported > as_of or val <= 0:
                continue
            prior = out.get(cik)
            if prior is None or reported > prior[1]:
                out[cik] = (val, reported, row.get("entityName", ""))
    logger.info("screen: share counts for %d filers as of %s", len(out), as_of)
    return out


def candidates(
    polygon, facts, edgar, as_of: date,
    min_adv: float = 10_000_000.0,
    min_price: float = 5.0, max_price: float = 2_000.0,
    cap_low: float = 2e9, cap_high: float = 20e9,
    max_turnover: float = MAX_TURNOVER,
) -> list[Candidate]:
    """Every name passing the liquidity and capitalisation filters at `as_of`."""
    liquidity = dollar_volume(polygon, as_of)
    listed = common_stock(polygon, as_of)
    shares = shares_outstanding(facts, as_of)
    cik_by_symbol = edgar.cik_map()

    out: list[Candidate] = []
    for symbol, (adv, price) in liquidity.items():
        sym = symbol.upper()
        if adv < min_adv or not (min_price <= price <= max_price):
            continue
        if listed and sym not in listed:
            continue
        cik = cik_by_symbol.get(sym)
        if cik is None:
            continue                     # foreign issuers, non-filers
        row = shares.get(cik)
        if row is None:
            continue
        count, reported, name = row
        cap = count * price
        if not (cap_low <= cap <= cap_high):
            continue
        c = Candidate(sym, cik, name, price, adv, count, cap, reported)
        if c.turnover > max_turnover:
            continue
        out.append(c)

    out.sort(key=lambda c: c.market_cap)
    logger.info("screen: %d names in $%.0fB–$%.0fB with ADV > $%.0fM",
                len(out), cap_low / 1e9, cap_high / 1e9, min_adv / 1e6)
    return out


def tradeable_trigger(facts, edgar, as_of: date, min_quarters: int = 12,
                      min_releases: int = 4):
    """An eligibility test: can this name produce a trigger at all?

    A universe slot spent on a company the trigger can never fire for is a slot
    that produces no trades and no information. Two ways that happens:

      no EPS history   fewer than three years of quarterly XBRL, so SUE has no
                       scale to standardise against
      no 8-K item 2.02 foreign private issuers file 20-F and 40-F and announce
                       results on 6-K, which carries no item codes. The join
                       drops every one of their quarters — correctly, but
                       silently, and the universe looks larger than it is

    Checked at selection time so the universe is honest about its own size.
    """
    def check(c: Candidate) -> bool:
        try:
            quarters = facts.quarters(c.symbol)
        except ProviderError:
            return False
        if len([q for q in quarters if q.end.date() <= as_of]) < min_quarters:
            logger.debug("screen: %s has too little EPS history", c.symbol)
            return False
        try:
            filings = edgar.earnings_releases(
                c.symbol,
                datetime.combine(as_of - timedelta(days=730), datetime.min.time(), UTC),
                datetime.combine(as_of, datetime.min.time(), UTC),
            )
        except ProviderError:
            return False
        n = sum(1 for f in filings if f.is_earnings_release)
        if n < min_releases:
            logger.debug("screen: %s files no results 8-K (%d)", c.symbol, n)
            return False
        return True

    return check


# SIC major groups to the coarse buckets the correlation cap already uses. The
# mapping is deliberately rough: a wrong-but-stable grouping still stops eight
# names in one industry being loaded as if they were eight independent bets,
# which is the only thing the cap is for.
_SIC_BUCKETS: tuple[tuple[int, int, str], ...] = (
    (100, 999, "materials"),        # agriculture
    (1000, 1099, "materials"),      # metal mining
    (1200, 1399, "energy"),
    (1400, 1499, "materials"),
    (1500, 1799, "industrials"),    # construction
    (2000, 2199, "staples"),        # food, tobacco
    (2200, 2399, "consumer_disc"),  # textiles, apparel
    (2400, 2599, "industrials"),
    (2600, 2699, "materials"),      # paper
    (2700, 2799, "consumer_disc"),  # publishing
    (2800, 2829, "materials"),      # industrial chemicals
    (2830, 2836, "healthcare"),     # pharma, biologics
    (2840, 2899, "staples"),        # soap, cosmetics
    (2900, 2999, "energy"),         # petroleum refining
    (3000, 3299, "materials"),
    (3300, 3399, "materials"),      # primary metals
    (3400, 3569, "industrials"),
    (3570, 3579, "tech_hw"),        # computers
    (3600, 3639, "industrials"),    # electrical equipment
    (3640, 3669, "tech_hw"),
    (3670, 3679, "semis"),
    (3680, 3699, "tech_hw"),
    (3700, 3799, "consumer_disc"),  # transport equipment
    (3800, 3829, "tech_hw"),        # instruments
    (3830, 3899, "healthcare"),     # medical devices
    (3900, 3999, "consumer_disc"),
    (4000, 4299, "industrials"),    # rail, trucking
    (4400, 4599, "industrials"),
    (4600, 4699, "energy"),         # pipelines
    (4700, 4799, "industrials"),
    (4800, 4899, "telecom"),
    (4900, 4999, "utilities"),
    (5000, 5199, "industrials"),    # wholesale
    (5200, 5599, "consumer_disc"),
    (5600, 5699, "consumer_disc"),
    (5700, 5799, "consumer_disc"),
    (5800, 5899, "consumer_disc"),  # restaurants
    (5900, 5999, "consumer_disc"),
    (6000, 6199, "financials"),
    (6200, 6299, "financials"),
    (6300, 6499, "insurance"),
    (6500, 6599, "reits"),
    (6700, 6799, "financials"),
    (7000, 7099, "consumer_disc"),
    (7200, 7299, "consumer_disc"),
    (7300, 7379, "tech_sw"),
    (7380, 7399, "industrials"),
    (7500, 7999, "consumer_disc"),
    (8000, 8099, "healthcare"),
    (8200, 8299, "consumer_disc"),
    (8700, 8799, "industrials"),    # engineering, consulting
)


def sector_for_sic(sic: Optional[str]) -> str:
    try:
        code = int(sic)
    except (TypeError, ValueError):
        return "unknown"
    for lo, hi, bucket in _SIC_BUCKETS:
        if lo <= code <= hi:
            return bucket
    return "unknown"


def sector_map(edgar, symbols: Iterable[str]) -> dict[str, str]:
    """symbol -> coarse sector, from the SIC code on the SEC submission.

    Without this every screened name resolves to "unknown" and the correlated
    exposure cap collapses the whole portfolio into ONE bucket: the run then
    holds two or three positions at a time and looks like a strategy that
    rarely fires, rather than a risk cap misconfigured by an empty map.
    """
    out: dict[str, str] = {}
    for symbol in symbols:
        cik = edgar.cik_for(symbol)
        if cik is None:
            out[symbol.upper()] = "unknown"
            continue
        try:
            payload = edgar._data.get(f"/submissions/CIK{cik}.json",
                                      tag=f"edgar-{symbol}")
        except ProviderError:
            out[symbol.upper()] = "unknown"
            continue
        out[symbol.upper()] = sector_for_sic((payload or {}).get("sic"))
    unknown = sum(1 for v in out.values() if v == "unknown")
    if unknown:
        logger.warning("sector map: %d of %d symbols unresolved", unknown, len(out))
    return out


def stratified(pool: Sequence[Candidate], count: int, eligible=None,
               max_tries: int = 6) -> list[Candidate]:
    """`count` names spread evenly across the capitalisation band.

    TWO CHOICES, BOTH FIXED BEFORE ANY BACKTEST.

    Stratify by market cap rather than take the top `count` by liquidity.
    Ranking on liquidity piles the universe into the large end of the band,
    which is safer to trade and destroys the one out-of-sample structural
    prediction the hypothesis makes — that drift weakens monotonically with
    capitalisation. That prediction needs spread across the band to be
    testable at all.

    Within a stratum, take the LOWEST-TURNOVER name, not the highest-volume
    one. This is the correction that matters. Picking the most-traded name in
    each stratum produced a universe of GameStop, Virgin Galactic, Beyond
    Meat, ChargePoint and Plug Power: 2021's most contested stories, whose
    prices were being argued over continuously by an enormous crowd. The
    hypothesis says the drift survives where repricing is SLOW and the
    counterparty is flow that moves over days. Selecting on turnover selects
    for the opposite, and would have tested a different strategy entirely.

    The ADV floor has already been applied to the pool, so "lowest turnover"
    cannot reach anything untradeable — it picks the quietest name that still
    clears $10M a day. It does mean the selected names sit nearer the floor
    than a liquidity-ranked universe would, and the cost model carries that.
    """
    if not pool or count <= 0:
        return []
    ordered = sorted(pool, key=lambda c: c.market_cap)
    if len(ordered) <= count and eligible is None:
        return ordered

    picked: list[Candidate] = []
    taken: set[str] = set()
    size = len(ordered) / count
    for i in range(count):
        lo, hi = int(i * size), max(int((i + 1) * size), int(i * size) + 1)
        stratum = sorted((c for c in ordered[lo:hi] if c.symbol not in taken),
                         key=lambda c: c.turnover)
        for c in stratum[:max_tries] if eligible else stratum[:1]:
            if eligible is None or eligible(c):
                picked.append(c)
                taken.add(c.symbol)
                break
        else:
            logger.info("screen: stratum %d (%.1f–%.1fB) yielded nothing eligible",
                        i, (ordered[lo].market_cap / 1e9 if lo < len(ordered) else 0),
                        (ordered[min(hi, len(ordered)) - 1].market_cap / 1e9))
    return picked


def describe(picked: Sequence[Candidate], pool_size: int, as_of: date) -> str:
    if not picked:
        return f"screen {as_of}: nothing passed"
    caps = [c.market_cap for c in picked]
    advs = [c.adv for c in picked]
    return (
        f"screen as of {as_of}: {len(picked)} of {pool_size} eligible\n"
        f"  market cap  ${min(caps)/1e9:.1f}B – ${max(caps)/1e9:.1f}B "
        f"(median ${sorted(caps)[len(caps)//2]/1e9:.1f}B)\n"
        f"  ADV         ${min(advs)/1e6:.0f}M – ${max(advs)/1e6:.0f}M "
        f"(median ${sorted(advs)[len(advs)//2]/1e6:.0f}M)"
    )


__all__ = ["Candidate", "candidates", "common_stock", "dollar_volume",
           "shares_outstanding", "stratified", "tradeable_trigger", "describe",
           "sector_map", "sector_for_sic",
           "ADV_SESSIONS", "MIN_SESSIONS", "MAX_TURNOVER"]
