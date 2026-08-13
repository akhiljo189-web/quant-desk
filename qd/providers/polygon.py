"""
qd.providers.polygon — market data, news, and the options tape.

Implements MarketData, NewsFeed and OptionsTape.

The options tape is the substantial part. Polygon serves option trades and
option quotes as separate endpoints, and the trades alone are nearly useless
for this purpose: without the NBBO that stood at the moment of each print you
cannot tell a buyer paying up from a seller hitting a bid, and unsigned volume
carries no direction. So this adapter fetches both and merges them — for each
trade, the last quote at or before its timestamp.

That merge is what makes `Aggressor` classification possible, and it is why
this file is longer than a thin API wrapper. Vendors that sell "options flow"
as a product are doing this same join and charging for it; doing it here keeps
the inputs auditable, which matters because the alternative is a pre-computed
sentiment score you cannot backtest and cannot inspect when it is wrong.

API budget: a full chain scan across a large universe is expensive. `contracts()`
filters by expiry and moneyness FIRST so trades are fetched only for the strikes
that could carry a directional signal. Watch the plan limits — a partial fetch
does not look like an error downstream, it looks like a quiet tape.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from qd.config import ProviderConfig
from qd.providers.base import MarketData, NewsFeed, OptionsTape, ProviderError
from qd.providers.http import HttpClient, RateLimiter, Recorder
from qd.types import (
    Bar, NewsItem, OptionContract, OptionTrade, Quote, Right, UTC, ensure_utc,
)

logger = logging.getLogger(__name__)

_NS = 1_000_000_000


def _from_ns(ns: int) -> datetime:
    return datetime.fromtimestamp(ns / _NS, tz=UTC)


def _from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC)


def parse_occ(occ: str) -> Optional[OptionContract]:
    """Parse an OCC symbol such as `O:AAPL260116C00200000`.

    Layout after the `O:` prefix is root + YYMMDD + C/P + strike in
    thousandths. The root is variable-length, so it is read backwards from the
    fixed 15-character tail rather than by assuming a width.
    """
    if occ.startswith("O:"):
        occ = occ[2:]
    if len(occ) < 16:
        return None
    tail = occ[-15:]
    root = occ[: -15]
    try:
        yy, mm, dd = int(tail[0:2]), int(tail[2:4]), int(tail[4:6])
        right = Right.CALL if tail[6].upper() == "C" else Right.PUT
        strike = int(tail[7:]) / 1000.0
        expiry = datetime(2000 + yy, mm, dd, 21, 0, tzinfo=UTC)   # 16:00 ET
    except (ValueError, IndexError):
        return None
    return OptionContract(
        underlying=root, expiry=expiry, strike=strike, right=right,
        occ_symbol=f"O:{occ}",
    )


class PolygonProvider:
    """Polygon.io adapter."""

    def __init__(self, cfg: ProviderConfig) -> None:
        if not cfg.polygon_api_key:
            raise ProviderError("POLYGON_API_KEY is not set")
        self.cfg = cfg
        self.http = HttpClient(
            base_url=cfg.polygon_base,
            headers={"Authorization": f"Bearer {cfg.polygon_api_key}"},
            timeout=cfg.request_timeout,
            max_retries=cfg.max_retries,
            rate_limiter=RateLimiter(cfg.rate_limit_per_min),
            recorder=Recorder(cfg.cache_dir, cfg.record_responses),
        )

    # ── paging ───────────────────────────────────────────────────────────────

    def _paged(self, path: str, params: dict, tag: str, cap: int = 20) -> list[dict]:
        """Follow next_url, with a hard page cap.

        The cap is a safety valve: an unbounded cursor loop against a busy
        symbol can burn an entire API quota inside one decision cycle and
        starve every other symbol for the rest of the session.
        """
        out: list[dict] = []
        payload = self.http.get(path, params, tag=tag)
        pages = 0
        while payload:
            out.extend(payload.get("results") or [])
            nxt = payload.get("next_url")
            pages += 1
            if not nxt or pages >= cap:
                if nxt:
                    logger.warning("%s: hit %d-page cap, results truncated", tag, cap)
                break
            payload = self.http.get(nxt, None, tag=tag)
        return out

    # ── MarketData ───────────────────────────────────────────────────────────

    def bars(
        self, symbol: str, start: datetime, end: datetime, minutes: int = 5
    ) -> list[Bar]:
        start, end = ensure_utc(start), ensure_utc(end)
        results = self._paged(
            f"/v2/aggs/ticker/{symbol.upper()}/range/{minutes}/minute/"
            f"{int(start.timestamp() * 1000)}/{int(end.timestamp() * 1000)}",
            {"adjusted": "true", "sort": "asc", "limit": 50000},
            tag=f"bars-{symbol}",
        )
        span = timedelta(minutes=minutes)
        out: list[Bar] = []
        for r in results:
            bar_start = _from_ms(r["t"])         # Polygon stamps the bar's START
            bar_end = bar_start + span
            # Drop a still-forming bar. Its close is not final, and acting on a
            # partial bar is look-ahead's twin: deciding on information that is
            # still changing.
            if bar_end > end:
                continue
            out.append(Bar(
                symbol=symbol.upper(), start=bar_start, end=bar_end,
                open=float(r["o"]), high=float(r["h"]), low=float(r["l"]),
                close=float(r["c"]), volume=float(r.get("v", 0.0)),
                vwap=float(r["vw"]) if r.get("vw") is not None else None,
                trades=int(r["n"]) if r.get("n") is not None else None,
            ))
        return out

    def daily_bars(self, symbol: str, start: datetime, end: datetime) -> list[Bar]:
        start, end = ensure_utc(start), ensure_utc(end)
        results = self._paged(
            f"/v2/aggs/ticker/{symbol.upper()}/range/1/day/"
            f"{start:%Y-%m-%d}/{end:%Y-%m-%d}",
            {"adjusted": "true", "sort": "asc", "limit": 5000},
            tag=f"daily-{symbol}",
        )
        out: list[Bar] = []
        for r in results:
            bar_start = _from_ms(r["t"])
            # A daily bar becomes known at the CLOSE of its session (21:00 UTC
            # in standard time). Stamping it with the session's start would make
            # the whole day's high, low and close available at 09:30.
            bar_end = bar_start.replace(hour=21, minute=0, second=0, microsecond=0)
            if bar_end <= bar_start:
                bar_end = bar_start + timedelta(hours=21)
            if bar_end > end:
                continue
            out.append(Bar(
                symbol=symbol.upper(), start=bar_start, end=bar_end,
                open=float(r["o"]), high=float(r["h"]), low=float(r["l"]),
                close=float(r["c"]), volume=float(r.get("v", 0.0)),
                vwap=float(r["vw"]) if r.get("vw") is not None else None,
            ))
        return out

    def quote(self, symbol: str, at: Optional[datetime] = None) -> Optional[Quote]:
        payload = self.http.get(
            f"/v2/last/nbbo/{symbol.upper()}", tag=f"quote-{symbol}"
        )
        if not payload or "results" not in payload:
            return None
        r = payload["results"]
        return Quote(
            symbol=symbol.upper(),
            ts=_from_ns(int(r.get("t", 0))) if r.get("t") else ensure_utc(at or datetime.now(UTC)),
            bid=float(r.get("p", 0.0)),
            ask=float(r.get("P", 0.0)),
            bid_size=float(r.get("s", 0.0)),
            ask_size=float(r.get("S", 0.0)),
        )

    # ── NewsFeed ─────────────────────────────────────────────────────────────

    def news(
        self, symbols: Sequence[str], since: datetime, until: datetime
    ) -> list[NewsItem]:
        since, until = ensure_utc(since), ensure_utc(until)
        out: list[NewsItem] = []
        seen: set[str] = set()

        for sym in symbols:
            results = self._paged(
                "/v2/reference/news",
                {
                    "ticker": sym.upper(),
                    "published_utc.gte": since.isoformat(),
                    "published_utc.lte": until.isoformat(),
                    "order": "asc", "limit": 50,
                },
                tag=f"news-{sym}", cap=3,
            )
            for r in results:
                nid = r.get("id") or r.get("article_url", "")
                if not nid or nid in seen:
                    continue
                seen.add(nid)
                published = ensure_utc(
                    datetime.fromisoformat(r["published_utc"].replace("Z", "+00:00"))
                )
                out.append(NewsItem(
                    id=nid,
                    symbols=tuple(r.get("tickers") or [sym.upper()]),
                    headline=r.get("title", ""),
                    summary=r.get("description", "") or "",
                    published_at=published,
                    # Historical pulls carry no true arrival time. Assuming
                    # instantaneous receipt would hand the backtest a latency
                    # advantage no live run can reproduce, so a conservative
                    # delay is applied instead. Live ingest overwrites this
                    # with the real arrival time.
                    received_at=published + timedelta(seconds=30),
                    source=(r.get("publisher") or {}).get("name", ""),
                    url=r.get("article_url", ""),
                ))
        return sorted(out, key=lambda n: n.known_at)

    # ── OptionsTape ──────────────────────────────────────────────────────────

    def contracts(
        self,
        underlying: str,
        as_of: datetime,
        spot: Optional[float] = None,
        max_dte: float = 90.0,
        max_abs_moneyness: float = 0.15,
        limit: int = 250,
    ) -> list[OptionContract]:
        """Contracts worth watching, filtered before any trade is fetched."""
        as_of = ensure_utc(as_of)
        results = self._paged(
            "/v3/reference/options/contracts",
            {
                "underlying_ticker": underlying.upper(),
                "as_of": as_of.strftime("%Y-%m-%d"),
                "expired": "false",
                "limit": limit,
            },
            tag=f"contracts-{underlying}", cap=4,
        )

        out: list[OptionContract] = []
        for r in results:
            try:
                expiry = datetime.strptime(r["expiration_date"], "%Y-%m-%d").replace(
                    hour=21, tzinfo=UTC
                )
            except (KeyError, ValueError):
                continue
            c = OptionContract(
                underlying=underlying.upper(), expiry=expiry,
                strike=float(r["strike_price"]),
                right=Right.CALL if r["contract_type"] == "call" else Right.PUT,
                occ_symbol=r.get("ticker", ""),
            )
            if c.dte(as_of) > max_dte:
                continue
            if spot and abs(c.moneyness(spot)) > max_abs_moneyness:
                continue
            out.append(c)
        return out

    def contract_trades(
        self,
        contract: OptionContract,
        start: datetime,
        end: datetime,
        spot: Optional[float] = None,
        open_interest: Optional[int] = None,
    ) -> list[OptionTrade]:
        """Trades for one contract, each joined to the NBBO that preceded it."""
        start, end = ensure_utc(start), ensure_utc(end)
        occ = contract.occ_symbol
        if not occ:
            return []

        raw_trades = self._paged(
            f"/v3/trades/{occ}",
            {
                "timestamp.gte": int(start.timestamp() * _NS),
                "timestamp.lte": int(end.timestamp() * _NS),
                "order": "asc", "limit": 5000,
            },
            tag=f"otrades-{contract.underlying}", cap=3,
        )
        if not raw_trades:
            return []

        raw_quotes = self._paged(
            f"/v3/quotes/{occ}",
            {
                "timestamp.gte": int(start.timestamp() * _NS),
                "timestamp.lte": int(end.timestamp() * _NS),
                "order": "asc", "limit": 5000,
            },
            tag=f"oquotes-{contract.underlying}", cap=3,
        )

        quotes = sorted(
            (
                (int(q["sip_timestamp"]), float(q.get("bid_price", 0.0)), float(q.get("ask_price", 0.0)))
                for q in raw_quotes if q.get("sip_timestamp")
            ),
            key=lambda x: x[0],
        )
        qts = [q[0] for q in quotes]

        import bisect

        out: list[OptionTrade] = []
        for t in raw_trades:
            ts_ns = int(t.get("sip_timestamp", 0))
            if not ts_ns:
                continue
            bid = ask = None
            # The quote in force is the last one at or BEFORE the trade. Taking
            # the nearest quote in either direction would let a quote that
            # moved in response to the trade decide who initiated it.
            i = bisect.bisect_right(qts, ts_ns) - 1
            if i >= 0:
                _, bid, ask = quotes[i]
                if bid <= 0 or ask <= 0:
                    bid = ask = None

            out.append(OptionTrade(
                contract=contract,
                ts=_from_ns(ts_ns),
                price=float(t.get("price", 0.0)),
                size=int(t.get("size", 0)),
                exchange=str(t.get("exchange", "")),
                conditions=tuple(str(c) for c in (t.get("conditions") or [])),
                nbbo_bid=bid, nbbo_ask=ask,
                underlying_price=spot,
                open_interest=open_interest,
                received_at=_from_ns(ts_ns) + timedelta(milliseconds=250),
            ))
        return out

    def option_trades(
        self, underlying: str, start: datetime, end: datetime
    ) -> list[OptionTrade]:
        """The full tape for an underlying across the relevant chain.

        Open interest comes from the chain snapshot, which reports the PRIOR
        session's close — the correct figure for judging whether today's prints
        are opening new risk.
        """
        start, end = ensure_utc(start), ensure_utc(end)
        spot, oi_map = self._snapshot(underlying)
        contracts = self.contracts(underlying, end, spot)

        out: list[OptionTrade] = []
        for c in contracts:
            out.extend(self.contract_trades(
                c, start, end, spot=spot, open_interest=oi_map.get(c.occ_symbol),
            ))
        return sorted(out, key=lambda t: t.known_at)

    def _snapshot(self, underlying: str) -> tuple[Optional[float], dict[str, int]]:
        """Spot price and per-contract open interest from the chain snapshot."""
        results = self._paged(
            f"/v3/snapshot/options/{underlying.upper()}",
            {"limit": 250}, tag=f"snapshot-{underlying}", cap=4,
        )
        spot: Optional[float] = None
        oi: dict[str, int] = {}
        for r in results:
            details = r.get("details") or {}
            ticker = details.get("ticker")
            if ticker and r.get("open_interest") is not None:
                oi[ticker] = int(r["open_interest"])
            if spot is None:
                ua = r.get("underlying_asset") or {}
                if ua.get("price"):
                    spot = float(ua["price"])
        return spot, oi


__all__ = ["PolygonProvider", "parse_occ"]
