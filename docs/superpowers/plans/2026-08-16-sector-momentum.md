# Sector Momentum Rotation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and honestly evaluate a monthly sector-rotation strategy that holds the four best-performing broad-market ETFs, moving to bonds or cash when a sector's own 12-month return turns negative.

**Architecture:** A new `rotation/` package inside the existing `quant-desk` repo. It reuses `qd.types.Bar` (the two-timestamp rule) and `qd.clock` (the NYSE calendar) but has its own data adapter, allocation engine, and benchmark-relative evaluator. It deliberately does NOT reuse `qd.risk`, `Intent`, or the existing `EdgeProof` gate, all of which assume per-trade stop-based risk that this strategy does not have.

**Tech Stack:** Python 3.11, standard library only (`urllib.request`, `json`, `csv`, `statistics`, `unittest`). No new third-party dependencies. Yahoo Finance chart API for data.

**Spec:** `docs/superpowers/specs/2026-08-16-sector-momentum-design.md`

## Global Constraints

- **Python 3.11+**, standard library only. No pandas, no numpy, no requests.
- **Every price record carries `known_at`.** A daily bar's `known_at` is its close, never its open. Reuse `qd.types.Bar`.
- **Frozen strategy parameters** — 12-month lookback, top 4 positions, monthly rebalance, absolute filter at 0%. These are set by convention and MUST NOT be changed to improve a result. Any change is a new spec.
- **Universe:** XLK XLF XLV XLY XLP XLE XLI XLB XLU XLRE XLC GLD IEF SHY. Benchmark SPY, never held.
- **Adjusted close only** (`adjclose`), so dividends are included.
- **Yahoo requests need a browser `User-Agent`**; without one the API returns 429.
- **Ties broken alphabetically by ticker**, so runs are reproducible.
- **Long only. No leverage. No stop losses. Equal weight at 25% per slot.**
- **Tests:** `python3 -m unittest discover -s tests -q` from the repo root. All existing tests must continue to pass.

---

### Task 1: Yahoo daily-bar adapter

**Files:**
- Create: `rotation/__init__.py`
- Create: `rotation/yahoo.py`
- Test: `tests/test_rotation_yahoo.py`

**Interfaces:**
- Consumes: `qd.types.Bar`, `qd.types.UTC` (existing).
- Produces:
  - `parse_chart(payload: dict, symbol: str) -> list[Bar]` — converts a Yahoo chart JSON payload into `Bar` objects sorted by date, skipping any row with a null adjusted close.
  - `YahooDaily(user_agent: str = DEFAULT_UA)` with method `bars(symbol: str) -> list[Bar]` — fetches full history.
  - `DEFAULT_UA: str`

**Background for the implementer:** The Yahoo endpoint is
`https://query1.finance.yahoo.com/v8/finance/chart/{SYMBOL}?period1=0&period2=2000000000&interval=1d&events=div%7Csplit&includeAdjustedClose=true`.
The response nests data as `chart.result[0]`, with parallel arrays: `timestamp`
(Unix seconds), `indicators.quote[0]` (`open`/`high`/`low`/`close`/`volume`) and
`indicators.adjclose[0].adjclose`. Arrays contain `null` on non-trading
anomalies. We store the **adjusted** close in `Bar.close` because every strategy
calculation is a total return; the unadjusted close is discarded.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_yahoo.py
"""
The Yahoo adapter, and the two rules that make its output trustworthy.

A daily bar's `known_at` must be its CLOSE. Stamping it with the open would
hand every downstream calculation a full session of foresight.

The adjusted close is what we keep. Sector ETFs pay meaningful dividends —
utilities and staples especially — so a price-only series understates every
holding and biases the comparison against SPY by an unpredictable amount.
"""

from __future__ import annotations

import unittest
from datetime import datetime

from qd.types import UTC
from rotation.yahoo import DEFAULT_UA, parse_chart


def payload(ts, opens, highs, lows, closes, adj, vols):
    return {"chart": {"result": [{
        "timestamp": ts,
        "indicators": {
            "quote": [{"open": opens, "high": highs, "low": lows,
                       "close": closes, "volume": vols}],
            "adjclose": [{"adjclose": adj}],
        },
    }]}}


class ParseChartTest(unittest.TestCase):
    def sample(self):
        # 2020-01-02 and 2020-01-03, UTC midnight
        return payload(
            ts=[1577923200, 1578009600],
            opens=[10.0, 11.0], highs=[10.5, 11.5], lows=[9.5, 10.5],
            closes=[10.2, 11.2], adj=[9.0, 10.0], vols=[1000.0, 2000.0],
        )

    def test_returns_one_bar_per_timestamp(self):
        bars = parse_chart(self.sample(), "XLK")
        self.assertEqual(len(bars), 2)
        self.assertEqual([b.symbol for b in bars], ["XLK", "XLK"])

    def test_close_is_the_adjusted_close(self):
        """Dividends are part of the return. The raw close is discarded."""
        bars = parse_chart(self.sample(), "XLK")
        self.assertEqual([b.close for b in bars], [9.0, 10.0])

    def test_known_at_is_the_bar_close_not_its_open(self):
        bars = parse_chart(self.sample(), "XLK")
        self.assertEqual(bars[0].known_at, bars[0].end)
        self.assertGreater(bars[0].end, bars[0].start)

    def test_rows_with_a_null_adjusted_close_are_dropped(self):
        """A null is missing data. Carrying it forward or zero-filling would
        invent a price nobody could have traded."""
        p = payload([1577923200, 1578009600], [10.0, 11.0], [10.5, 11.5],
                    [9.5, 10.5], [10.2, 11.2], [9.0, None], [1000.0, 2000.0])
        self.assertEqual(len(parse_chart(p, "XLK")), 1)

    def test_bars_come_back_in_date_order(self):
        p = payload([1578009600, 1577923200], [11.0, 10.0], [11.5, 10.5],
                    [10.5, 9.5], [11.2, 10.2], [10.0, 9.0], [2000.0, 1000.0])
        bars = parse_chart(p, "XLK")
        self.assertLess(bars[0].end, bars[1].end)

    def test_an_empty_result_is_an_empty_list_not_a_crash(self):
        self.assertEqual(parse_chart({"chart": {"result": []}}, "XLK"), [])

    def test_a_default_user_agent_exists(self):
        """Yahoo returns 429 to a client with no browser User-Agent."""
        self.assertIn("Mozilla", DEFAULT_UA)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_yahoo -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rotation'`

- [ ] **Step 3: Write minimal implementation**

```python
# rotation/__init__.py
"""Sector momentum rotation. See docs/superpowers/specs/2026-08-16-sector-momentum-design.md"""
```

```python
# rotation/yahoo.py
"""
rotation.yahoo — daily total-return history from the Yahoo chart API.

Why not the existing Polygon adapter: the data plan holds five years. A
monthly strategy tested over five years is sixty decisions from a single
regime, which cannot distinguish a strategy from luck. Yahoo reaches back to
each ETF's inception — 1998 for the original sector funds — which covers the
dot-com crash, 2008, 2018, 2020 and 2022. Those are the periods where the
defensive filter either works or does not, and that is the whole question.

Stooq was the first choice and is unusable: it now serves a JavaScript
proof-of-work bot challenge instead of CSV, so an HTTP client gets an HTML
page. Defeating that needs a headless browser, which is not a dependency
worth taking for a data fetch.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Optional

from qd.types import UTC, Bar

logger = logging.getLogger(__name__)

BASE = "https://query1.finance.yahoo.com/v8/finance/chart"

# Yahoo answers 429 to a client that does not look like a browser.
DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36")


def parse_chart(payload: dict, symbol: str) -> list[Bar]:
    """Yahoo chart JSON -> Bars, oldest first.

    The ADJUSTED close becomes `Bar.close` and the raw close is discarded.
    Every calculation downstream is a total return, and sector ETFs pay real
    dividends — utilities and staples especially. A price-only series would
    understate every holding while SPY's benchmark series stayed correct,
    biasing the comparison in a direction nobody could predict.
    """
    results = ((payload or {}).get("chart") or {}).get("result") or []
    if not results:
        return []
    r = results[0]
    stamps = r.get("timestamp") or []
    quote = (r.get("indicators", {}).get("quote") or [{}])[0]
    adj = (r.get("indicators", {}).get("adjclose") or [{}])[0].get("adjclose") or []

    out: list[Bar] = []
    for i, stamp in enumerate(stamps):
        def at(seq, default=0.0):
            v = seq[i] if i < len(seq) else None
            return default if v is None else float(v)

        close = adj[i] if i < len(adj) else None
        if close is None:
            # A null is missing data. Carrying the previous value forward, or
            # zero-filling, invents a price nobody could have traded.
            continue
        end = datetime.fromtimestamp(int(stamp), UTC)
        out.append(Bar(
            symbol=symbol.upper(),
            start=end - timedelta(days=1),
            end=end,                      # known_at is the CLOSE
            open=at(quote.get("open") or []),
            high=at(quote.get("high") or []),
            low=at(quote.get("low") or []),
            close=float(close),
            volume=at(quote.get("volume") or []),
        ))
    out.sort(key=lambda b: b.end)
    return out


class YahooDaily:
    """Full daily history for a symbol, adjusted for dividends and splits."""

    def __init__(self, user_agent: str = DEFAULT_UA, timeout: float = 60.0) -> None:
        self.user_agent = user_agent
        self.timeout = timeout

    def bars(self, symbol: str) -> list[Bar]:
        url = (f"{BASE}/{symbol.upper()}?period1=0&period2=2000000000"
               f"&interval=1d&events=div%7Csplit&includeAdjustedClose=true")
        req = urllib.request.Request(url, headers={"User-Agent": self.user_agent})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ValueError) as exc:
            logger.warning("%s: yahoo fetch failed: %s", symbol, exc)
            return []
        return parse_chart(payload, symbol)


__all__ = ["YahooDaily", "parse_chart", "DEFAULT_UA", "BASE"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_yahoo -v`
Expected: PASS, 7 tests

- [ ] **Step 5: Verify against the live API**

Run:
```bash
cd /home/user/quant-desk && python3 -c "
from rotation.yahoo import YahooDaily
bars = YahooDaily().bars('XLK')
print(len(bars), bars[0].end.date(), '->', bars[-1].end.date())
assert len(bars) > 6000, 'expected 27 years of history'
assert bars[0].end.year == 1998, bars[0].end
print('OK')
"
```
Expected: about 6,953 bars, `1998-12-22 -> 2026-08-14`, then `OK`

- [ ] **Step 6: Commit**

```bash
cd /home/user/quant-desk
git add rotation/__init__.py rotation/yahoo.py tests/test_rotation_yahoo.py
git commit -m "Add Yahoo daily adapter for long-history ETF data"
```

---

### Task 2: Archive builder and loader

**Files:**
- Create: `rotation/archive.py`
- Test: `tests/test_rotation_archive.py`

**Interfaces:**
- Consumes: `rotation.yahoo.YahooDaily`, `rotation.yahoo.parse_chart`, `qd.types.Bar`.
- Produces:
  - `UNIVERSE: tuple[str, ...]` — the 14 tradeable tickers.
  - `BENCHMARK: str` — `"SPY"`.
  - `build(root: str, source=None, symbols=None) -> dict[str, int]` — fetches and writes one JSONL file per symbol, returning `{symbol: bar_count}`.
  - `load(root: str) -> dict[str, list[Bar]]` — reads them back.

**Background:** JSONL on disk, one file per symbol, so a partial fetch is
resumable and a symbol can be re-fetched without touching the rest. The round
trip must preserve `end` exactly — it is the `known_at` that the whole
point-in-time guarantee rests on.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_archive.py
"""
The archive round trip.

`Bar.end` is `known_at`. If saving and loading perturbs it by even a second,
every point-in-time guarantee downstream is quietly false.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from qd.types import UTC, Bar
from rotation.archive import BENCHMARK, UNIVERSE, build, load


def bar(symbol, day, close):
    end = datetime(2020, 1, day, tzinfo=UTC)
    return Bar(symbol=symbol, start=end - timedelta(days=1), end=end,
               open=close, high=close, low=close, close=close, volume=100.0)


class FakeSource:
    def __init__(self, data):
        self.data = data
        self.asked = []

    def bars(self, symbol):
        self.asked.append(symbol)
        return list(self.data.get(symbol, []))


class UniverseTest(unittest.TestCase):
    def test_universe_is_the_fourteen_from_the_spec(self):
        self.assertEqual(len(UNIVERSE), 14)
        for t in ("XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB",
                  "XLU", "XLRE", "XLC", "GLD", "IEF", "SHY"):
            self.assertIn(t, UNIVERSE)

    def test_the_benchmark_is_never_in_the_tradeable_universe(self):
        """SPY is what we are measured against. Holding it would make the
        comparison meaningless."""
        self.assertEqual(BENCHMARK, "SPY")
        self.assertNotIn(BENCHMARK, UNIVERSE)


class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()

    def test_build_then_load_preserves_known_at_exactly(self):
        src = FakeSource({"XLK": [bar("XLK", 2, 10.0), bar("XLK", 3, 11.0)]})
        build(self.root, source=src, symbols=("XLK",))
        loaded = load(self.root)
        self.assertEqual([b.end for b in loaded["XLK"]],
                         [bar("XLK", 2, 10.0).end, bar("XLK", 3, 11.0).end])
        self.assertEqual([b.known_at for b in loaded["XLK"]],
                         [b.end for b in loaded["XLK"]])

    def test_build_reports_counts_per_symbol(self):
        src = FakeSource({"XLK": [bar("XLK", 2, 10.0)], "GLD": []})
        counts = build(self.root, source=src, symbols=("XLK", "GLD"))
        self.assertEqual(counts, {"XLK": 1, "GLD": 0})

    def test_closes_survive_the_round_trip(self):
        src = FakeSource({"XLK": [bar("XLK", 2, 123.456)]})
        build(self.root, source=src, symbols=("XLK",))
        self.assertAlmostEqual(load(self.root)["XLK"][0].close, 123.456)

    def test_loading_an_empty_directory_is_not_an_error(self):
        self.assertEqual(load(tempfile.mkdtemp()), {})

    def test_build_fetches_the_benchmark_too(self):
        src = FakeSource({})
        build(self.root, source=src)
        self.assertIn(BENCHMARK, src.asked)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_archive -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rotation.archive'`

- [ ] **Step 3: Write minimal implementation**

```python
# rotation/archive.py
"""
rotation.archive — fetch once, replay many times.

One JSONL file per symbol so a partial fetch resumes and a single symbol can
be refreshed without disturbing the rest.

`Bar.end` is written and read as an ISO timestamp and must round-trip
exactly: it IS `known_at`, and every point-in-time guarantee downstream
rests on it being the moment the bar closed rather than any nearby moment.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Iterable, Optional

from qd.types import UTC, Bar

logger = logging.getLogger(__name__)

# The eleven GICS sectors plus three defensive assets. Broad and long-lived by
# design: thematic ETFs are launched after a theme is already hot and delisted
# when it dies, so a universe of them is selected on the outcome. See the spec.
UNIVERSE: tuple[str, ...] = (
    "XLK", "XLF", "XLV", "XLY", "XLP", "XLE", "XLI", "XLB", "XLU", "XLRE",
    "XLC", "GLD", "IEF", "SHY",
)

# Measured against, never held.
BENCHMARK = "SPY"


def _path(root: str, symbol: str) -> str:
    return os.path.join(root, f"{symbol.upper()}.jsonl")


def build(root: str, source=None, symbols: Optional[Iterable[str]] = None
          ) -> dict[str, int]:
    """Fetch each symbol and write it to `root`. Returns bar counts."""
    if source is None:
        from rotation.yahoo import YahooDaily
        source = YahooDaily()
    wanted = tuple(symbols) if symbols is not None else UNIVERSE + (BENCHMARK,)
    os.makedirs(root, exist_ok=True)

    counts: dict[str, int] = {}
    for symbol in wanted:
        bars = source.bars(symbol)
        with open(_path(root, symbol), "w") as fh:
            for b in bars:
                fh.write(json.dumps({
                    "symbol": b.symbol,
                    "start": b.start.isoformat(),
                    "end": b.end.isoformat(),
                    "o": b.open, "h": b.high, "l": b.low, "c": b.close,
                    "v": b.volume,
                }) + "\n")
        counts[symbol] = len(bars)
        logger.info("%s: %d bars", symbol, len(bars))
    return counts


def load(root: str) -> dict[str, list[Bar]]:
    """Read an archive back. Missing directory means an empty archive."""
    out: dict[str, list[Bar]] = {}
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        if not name.endswith(".jsonl"):
            continue
        symbol = name[:-6]
        bars: list[Bar] = []
        with open(os.path.join(root, name)) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                bars.append(Bar(
                    symbol=d["symbol"],
                    start=datetime.fromisoformat(d["start"]),
                    end=datetime.fromisoformat(d["end"]),
                    open=d["o"], high=d["h"], low=d["l"], close=d["c"],
                    volume=d["v"],
                ))
        bars.sort(key=lambda b: b.end)
        out[symbol] = bars
    return out


__all__ = ["UNIVERSE", "BENCHMARK", "build", "load"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_archive -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Build the real archive**

Run:
```bash
cd /home/user/quant-desk && python3 -c "
import logging; logging.basicConfig(level=logging.INFO, format='%(message)s')
from rotation.archive import build
counts = build('data/rotation')
print(sum(counts.values()), 'bars total across', len(counts), 'symbols')
assert counts['XLK'] > 6000, counts
assert counts['SPY'] > 8000, counts
print('OK')
"
```
Expected: roughly 78,000 bars across 15 symbols, then `OK`

- [ ] **Step 6: Commit**

```bash
cd /home/user/quant-desk
git add rotation/archive.py tests/test_rotation_archive.py
git commit -m "Add rotation archive builder and loader"
```

---

### Task 3: Month-end index and 12-month momentum

**Files:**
- Create: `rotation/momentum.py`
- Test: `tests/test_rotation_momentum.py`

**Interfaces:**
- Consumes: `qd.types.Bar`.
- Produces:
  - `month_ends(bars: list[Bar]) -> list[Bar]` — the last bar of each calendar month.
  - `MonthlySeries(bars: list[Bar])` with `.close_on_or_before(when: datetime) -> Optional[float]` and `.first_date -> Optional[datetime]`.
  - `twelve_month_return(series: MonthlySeries, asof: datetime) -> Optional[float]` — fractional total return, or `None` when twelve months of history are unavailable.

**Background:** This is where look-ahead would enter. `asof` is the last
trading day of the prior month; the function may use no bar closing after it.
Returning `None` rather than a partial-window return is deliberate — a
six-month return silently compared against twelve-month returns would rank a
new entrant on a different scale from everything else.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_momentum.py
"""
Momentum measurement, and the look-ahead it would be easy to introduce.

`asof` is the last trading day of the PRIOR month. Any bar closing after it
is the future. The strategy's entire honesty rests on this one boundary.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.types import UTC, Bar
from rotation.momentum import MonthlySeries, month_ends, twelve_month_return


def daily(symbol, start_year, months, close_fn):
    """One bar on the 1st, 15th and 28th of each month."""
    out = []
    for m in range(months):
        year, month = start_year + m // 12, m % 12 + 1
        for day in (1, 15, 28):
            end = datetime(year, month, day, tzinfo=UTC)
            c = close_fn(m, day)
            out.append(Bar(symbol=symbol, start=end - timedelta(days=1), end=end,
                           open=c, high=c, low=c, close=c, volume=100.0))
    return out


class MonthEndsTest(unittest.TestCase):
    def test_one_bar_per_month_and_it_is_the_last(self):
        bars = daily("XLK", 2020, 3, lambda m, d: 10.0 + m + d / 100)
        ends = month_ends(bars)
        self.assertEqual(len(ends), 3)
        self.assertEqual([b.end.day for b in ends], [28, 28, 28])

    def test_empty_input_gives_empty_output(self):
        self.assertEqual(month_ends([]), [])


class TwelveMonthReturnTest(unittest.TestCase):
    def series(self, close_fn, months=26):
        return MonthlySeries(daily("XLK", 2020, months, close_fn))

    def test_a_doubling_over_twelve_months_reads_as_plus_one(self):
        # month 0 close 100, month 12 close 200
        s = self.series(lambda m, d: 100.0 * (2 ** (m / 12)))
        r = twelve_month_return(s, datetime(2021, 1, 28, tzinfo=UTC))
        self.assertAlmostEqual(r, 1.0, places=2)

    def test_a_flat_series_reads_as_zero(self):
        s = self.series(lambda m, d: 50.0)
        self.assertAlmostEqual(
            twelve_month_return(s, datetime(2021, 6, 28, tzinfo=UTC)), 0.0)

    def test_a_fall_reads_as_negative(self):
        s = self.series(lambda m, d: 100.0 - m)
        r = twelve_month_return(s, datetime(2021, 6, 28, tzinfo=UTC))
        self.assertLess(r, 0.0)

    def test_it_never_reads_a_bar_after_asof(self):
        """THE look-ahead test. Prices explode after June 2021; a June
        measurement must not see any of it."""
        s = self.series(lambda m, d: 100.0 if m < 18 else 100_000.0)
        r = twelve_month_return(s, datetime(2021, 6, 28, tzinfo=UTC))
        self.assertAlmostEqual(r, 0.0, places=6)

    def test_too_little_history_returns_none_not_a_short_window(self):
        """XLRE and XLC join the universe mid-sample. Ranking a six-month
        return against twelve-month returns compares different things."""
        s = self.series(lambda m, d: 100.0, months=6)
        self.assertIsNone(twelve_month_return(s, datetime(2020, 6, 28, tzinfo=UTC)))

    def test_an_empty_series_returns_none(self):
        self.assertIsNone(
            twelve_month_return(MonthlySeries([]), datetime(2021, 6, 1, tzinfo=UTC)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_momentum -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rotation.momentum'`

- [ ] **Step 3: Write minimal implementation**

```python
# rotation/momentum.py
"""
rotation.momentum — month-end sampling and the 12-month total return.

This module is where look-ahead would enter the strategy, so the boundary is
explicit rather than assumed: `asof` is the last trading day of the PRIOR
month, and no bar closing after it may be read.

`twelve_month_return` returns None rather than a shorter-window return when
history is thin. XLRE joins the universe in 2015 and XLC in 2018; ranking
their six-month returns against everything else's twelve-month returns would
compare two different quantities and systematically favour whichever
happened to be measured over the shorter, quieter window.
"""

from __future__ import annotations

import bisect
from datetime import datetime, timedelta
from typing import Optional

from qd.types import Bar

# A calendar year back, with a tolerance. Month-end dates never land exactly
# 365 days apart, so the lookup takes the last close at or before the target.
YEAR = timedelta(days=365)


def month_ends(bars: list[Bar]) -> list[Bar]:
    """The last bar of each calendar month, oldest first."""
    by_month: dict[tuple[int, int], Bar] = {}
    for b in bars:
        key = (b.end.year, b.end.month)
        prior = by_month.get(key)
        if prior is None or b.end > prior.end:
            by_month[key] = b
    return [by_month[k] for k in sorted(by_month)]


class MonthlySeries:
    """Month-end closes for one symbol, with as-of lookup."""

    def __init__(self, bars: list[Bar]) -> None:
        self.bars = month_ends(bars)
        self._dates = [b.end for b in self.bars]

    @property
    def first_date(self) -> Optional[datetime]:
        return self._dates[0] if self._dates else None

    def close_on_or_before(self, when: datetime) -> Optional[float]:
        """The most recent month-end close at or before `when`.

        Strictly at or before: this is the look-ahead boundary.
        """
        i = bisect.bisect_right(self._dates, when)
        return self.bars[i - 1].close if i else None


def twelve_month_return(series: MonthlySeries, asof: datetime) -> Optional[float]:
    """Fractional total return over the twelve months ending at `asof`.

    None when a full twelve months is not available.
    """
    if series.first_date is None or asof - series.first_date < YEAR:
        return None
    now = series.close_on_or_before(asof)
    then = series.close_on_or_before(asof - YEAR)
    if not now or not then or then <= 0:
        return None
    return now / then - 1.0


__all__ = ["month_ends", "MonthlySeries", "twelve_month_return", "YEAR"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_momentum -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
cd /home/user/quant-desk
git add rotation/momentum.py tests/test_rotation_momentum.py
git commit -m "Add month-end sampling and 12-month momentum"
```

---

### Task 4: Selection rules

**Files:**
- Create: `rotation/select.py`
- Test: `tests/test_rotation_select.py`

**Interfaces:**
- Consumes: `rotation.momentum.MonthlySeries`, `rotation.momentum.twelve_month_return`, `rotation.archive.UNIVERSE`.
- Produces:
  - `TOP_N: int = 4`, `WEIGHT: float = 0.25`, `DEFENSIVE: tuple[str, ...] = ("IEF", "SHY")`
  - `Ranking` dataclass with fields `symbol: str` and `ret: float`.
  - `rank(series_by_symbol: dict[str, MonthlySeries], asof: datetime) -> list[Ranking]` — eligible symbols ranked best first, ties broken alphabetically.
  - `select(series_by_symbol: dict[str, MonthlySeries], asof: datetime) -> dict[str, float]` — target weights summing to 1.0 (or less only when no defensive asset qualifies).

**Background — the rules, verbatim from the spec:** rank by 12-month total
return; hold the top 4 at 25% each; a slot whose asset has a negative
12-month return goes to IEF, and to SHY if IEF is also negative; ties break
alphabetically so two runs of the same backtest agree.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_select.py
"""
The selection rules.

Four parameters — 12-month lookback, top 4, 25% each, filter at zero — set by
published convention and frozen before the first backtest. The previous
strategy in this repo produced a coin flip that tuning would have "improved"
trivially; these tests exist so a later change is loud rather than quiet.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.types import UTC, Bar
from rotation.momentum import MonthlySeries
from rotation.select import DEFENSIVE, TOP_N, WEIGHT, rank, select

ASOF = datetime(2022, 1, 31, tzinfo=UTC)


def series_returning(pct):
    """A monthly series whose 12-month return at ASOF is `pct` (fractional)."""
    bars = []
    start = datetime(2019, 1, 31, tzinfo=UTC)
    for m in range(40):
        end = start + timedelta(days=30 * m)
        # flat until 24 months in, then a single step to produce the return
        c = 100.0 if m < 24 else 100.0 * (1 + pct)
        bars.append(Bar(symbol="X", start=end - timedelta(days=1), end=end,
                        open=c, high=c, low=c, close=c, volume=1.0))
    return MonthlySeries(bars)


def universe(**kw):
    return {sym: series_returning(pct) for sym, pct in kw.items()}


class RankTest(unittest.TestCase):
    def test_best_first(self):
        u = universe(XLK=0.30, XLE=0.10, XLF=0.20)
        self.assertEqual([r.symbol for r in rank(u, ASOF)], ["XLK", "XLF", "XLE"])

    def test_ties_break_alphabetically_so_runs_are_reproducible(self):
        u = universe(XLV=0.10, XLB=0.10, XLE=0.10)
        self.assertEqual([r.symbol for r in rank(u, ASOF)], ["XLB", "XLE", "XLV"])

    def test_symbols_without_twelve_months_are_not_ranked(self):
        short = MonthlySeries([])
        u = universe(XLK=0.10)
        u["XLC"] = short
        self.assertEqual([r.symbol for r in rank(u, ASOF)], ["XLK"])


class SelectTest(unittest.TestCase):
    def test_holds_the_top_four_at_equal_weight(self):
        u = universe(XLK=0.50, XLF=0.40, XLV=0.30, XLY=0.20, XLE=0.10, XLB=0.05)
        w = select(u, ASOF)
        self.assertEqual(set(w), {"XLK", "XLF", "XLV", "XLY"})
        for v in w.values():
            self.assertAlmostEqual(v, WEIGHT)
        self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_a_falling_asset_is_replaced_by_bonds(self):
        """The absolute filter. A slot whose asset lost money over the year
        holds bonds instead, however well it ranks."""
        u = universe(XLK=0.50, XLF=0.40, XLV=-0.10, XLY=-0.20, IEF=0.05)
        w = select(u, ASOF)
        self.assertEqual(set(w), {"XLK", "XLF", "IEF"})
        self.assertAlmostEqual(w["IEF"], 0.50)      # two slots merged
        self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_cash_when_even_bonds_are_falling(self):
        u = universe(XLK=-0.10, XLF=-0.20, IEF=-0.05, SHY=0.01)
        w = select(u, ASOF)
        self.assertEqual(set(w), {"SHY"})
        self.assertAlmostEqual(w["SHY"], 1.0)

    def test_everything_falling_leaves_the_book_in_cash(self):
        u = universe(XLK=-0.10, XLF=-0.20, IEF=-0.05, SHY=-0.01)
        w = select(u, ASOF)
        self.assertEqual(set(w), {"SHY"})

    def test_defensive_assets_can_be_held_on_their_own_merit(self):
        """Gold or bonds rising while sectors fall is a legitimate top-four
        finish, not a fallback."""
        u = universe(GLD=0.40, IEF=0.20, XLK=-0.10, XLF=-0.20)
        w = select(u, ASOF)
        self.assertIn("GLD", w)
        self.assertAlmostEqual(sum(w.values()), 1.0)

    def test_the_frozen_parameters_are_what_the_spec_says(self):
        self.assertEqual(TOP_N, 4)
        self.assertAlmostEqual(WEIGHT, 0.25)
        self.assertEqual(DEFENSIVE, ("IEF", "SHY"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_select -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rotation.select'`

- [ ] **Step 3: Write minimal implementation**

```python
# rotation/select.py
"""
rotation.select — rank, filter, and turn the result into target weights.

FOUR PARAMETERS, FROZEN BEFORE THE FIRST BACKTEST. Twelve-month lookback,
top four, equal weight, absolute filter at zero. Every one is a published
convention rather than a fitted value, and none may be changed to improve a
result — a change is a new hypothesis needing a new spec and a fresh
out-of-sample test. The previous strategy in this repository finished at
+0.016R with a t-statistic of 0.24, a coin flip that a few hours of parameter
search would have turned into a convincing-looking backtest.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from rotation.momentum import MonthlySeries, twelve_month_return

TOP_N = 4
WEIGHT = 0.25
# Tried in order when a slot fails the absolute filter.
DEFENSIVE: tuple[str, ...] = ("IEF", "SHY")


@dataclass(frozen=True)
class Ranking:
    symbol: str
    ret: float


def rank(series_by_symbol: dict[str, MonthlySeries], asof: datetime) -> list[Ranking]:
    """Eligible symbols, best first. Ties break alphabetically.

    An arbitrary tie-break is still better than an unspecified one: two runs
    of the same backtest must produce identical results.
    """
    rows: list[Ranking] = []
    for symbol in sorted(series_by_symbol):
        r = twelve_month_return(series_by_symbol[symbol], asof)
        if r is not None:
            rows.append(Ranking(symbol, r))
    rows.sort(key=lambda x: (-x.ret, x.symbol))
    return rows


def _defensive_pick(series_by_symbol, asof) -> Optional[str]:
    """First defensive asset with a non-negative 12-month return."""
    for symbol in DEFENSIVE:
        series = series_by_symbol.get(symbol)
        if series is None:
            continue
        r = twelve_month_return(series, asof)
        if r is not None and r > 0:
            return symbol
    # Everything is falling. The last defensive asset is the cash equivalent
    # and is where capital sits rather than staying in a falling market.
    return DEFENSIVE[-1] if DEFENSIVE[-1] in series_by_symbol else None


def select(series_by_symbol: dict[str, MonthlySeries], asof: datetime
           ) -> dict[str, float]:
    """Target weights for the coming month.

    Weights sum to 1.0. Slots failing the absolute filter merge into the
    defensive asset, so a month with two failing slots holds 50% bonds
    rather than two separate 25% lines of the same fund.
    """
    ranked = rank(series_by_symbol, asof)
    weights: dict[str, float] = {}
    fallback_slots = 0

    for row in ranked[:TOP_N]:
        if row.ret > 0:
            weights[row.symbol] = weights.get(row.symbol, 0.0) + WEIGHT
        else:
            fallback_slots += 1

    # Fewer eligible assets than slots also falls back, rather than
    # concentrating the book into whatever happens to exist.
    fallback_slots += max(0, TOP_N - len(ranked))

    if fallback_slots:
        pick = _defensive_pick(series_by_symbol, asof)
        if pick:
            weights[pick] = weights.get(pick, 0.0) + WEIGHT * fallback_slots
    return weights


__all__ = ["rank", "select", "Ranking", "TOP_N", "WEIGHT", "DEFENSIVE"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_select -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
cd /home/user/quant-desk
git add rotation/select.py tests/test_rotation_select.py
git commit -m "Add rotation selection rules with absolute momentum filter"
```

---

### Task 5: Monthly backtest engine

**Files:**
- Create: `rotation/backtest.py`
- Test: `tests/test_rotation_backtest.py`

**Interfaces:**
- Consumes: `rotation.momentum.MonthlySeries`, `rotation.select.select`, `rotation.archive.BENCHMARK`, `qd.types.Bar`.
- Produces:
  - `Result` dataclass: `equity: list[tuple[datetime, float]]`, `weights_by_month: list[tuple[datetime, dict[str, float]]]`, `turnover: list[float]`, with methods `cagr() -> float`, `max_drawdown_pct() -> float`, `sharpe() -> float`, `final() -> float`.
  - `run(bars_by_symbol: dict[str, list[Bar]], start: datetime, end: datetime, equity: float = 10_000.0, monthly_contribution: float = 0.0, cost_bps: float = 10.0) -> Result`
  - `buy_and_hold(bars: list[Bar], start, end, equity=10_000.0, monthly_contribution=0.0) -> Result`

**Background:** Signals are computed at each month end; the resulting weights
apply to the following month. Costs are charged on turnover — the fraction of
the book that changes — at `cost_bps` per unit traded. 10bp is the default:
LSE UCITS ETF spreads run 3–10bp, and the round trip is charged once here.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_backtest.py
"""
The backtest loop.

Two properties matter more than the arithmetic: a month's weights are decided
from data available at the PREVIOUS month end, and costs are charged on what
actually changes rather than on the whole book every month.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.types import UTC, Bar
from rotation.backtest import Result, buy_and_hold, run


def ramp(symbol, months, start_close, monthly_growth):
    bars, c = [], start_close
    base = datetime(2015, 1, 31, tzinfo=UTC)
    for m in range(months):
        end = base + timedelta(days=30 * m)
        bars.append(Bar(symbol=symbol, start=end - timedelta(days=1), end=end,
                        open=c, high=c, low=c, close=c, volume=1.0))
        c *= (1 + monthly_growth)
    return bars


START = datetime(2016, 6, 1, tzinfo=UTC)
END = datetime(2020, 1, 1, tzinfo=UTC)


class BuyAndHoldTest(unittest.TestCase):
    def test_a_rising_market_compounds(self):
        r = buy_and_hold(ramp("SPY", 70, 100.0, 0.01), START, END, equity=1000.0)
        self.assertGreater(r.final(), 1000.0)

    def test_a_flat_market_preserves_capital(self):
        r = buy_and_hold(ramp("SPY", 70, 100.0, 0.0), START, END, equity=1000.0)
        self.assertAlmostEqual(r.final(), 1000.0, places=6)

    def test_contributions_are_added(self):
        flat = ramp("SPY", 70, 100.0, 0.0)
        r = buy_and_hold(flat, START, END, equity=1000.0, monthly_contribution=100.0)
        self.assertGreater(r.final(), 1000.0)


class RunTest(unittest.TestCase):
    def universe(self):
        return {
            "XLK": ramp("XLK", 70, 100.0, 0.02),     # best
            "XLF": ramp("XLF", 70, 100.0, 0.01),
            "XLV": ramp("XLV", 70, 100.0, 0.005),
            "XLE": ramp("XLE", 70, 100.0, 0.002),
            "XLU": ramp("XLU", 70, 100.0, 0.001),    # worst, excluded
            "IEF": ramp("IEF", 70, 100.0, 0.0005),
            "SHY": ramp("SHY", 70, 100.0, 0.0001),
        }

    def test_it_holds_the_strongest_assets(self):
        r = run(self.universe(), START, END, equity=1000.0)
        _, w = r.weights_by_month[-1]
        self.assertIn("XLK", w)
        self.assertNotIn("XLU", w)

    def test_equity_grows_in_a_rising_universe(self):
        r = run(self.universe(), START, END, equity=1000.0)
        self.assertGreater(r.final(), 1000.0)

    def test_costs_reduce_the_result(self):
        cheap = run(self.universe(), START, END, equity=1000.0, cost_bps=0.0)
        dear = run(self.universe(), START, END, equity=1000.0, cost_bps=100.0)
        self.assertLess(dear.final(), cheap.final())

    def test_a_stable_book_pays_almost_no_cost(self):
        """Costs are charged on turnover. A month with no change must not be
        charged as though the whole book were traded."""
        r = run(self.universe(), START, END, equity=1000.0, cost_bps=100.0)
        self.assertLess(sum(r.turnover[2:]) / max(1, len(r.turnover[2:])), 0.2)

    def test_a_falling_universe_moves_to_defence(self):
        falling = {k: ramp(k, 70, 100.0, -0.01) for k in
                   ("XLK", "XLF", "XLV", "XLE")}
        falling["IEF"] = ramp("IEF", 70, 100.0, 0.002)
        falling["SHY"] = ramp("SHY", 70, 100.0, 0.0001)
        r = run(falling, START, END, equity=1000.0)
        _, w = r.weights_by_month[-1]
        self.assertTrue(set(w) <= {"IEF", "SHY"}, w)

    def test_metrics_are_computable(self):
        r = run(self.universe(), START, END, equity=1000.0)
        self.assertGreater(r.cagr(), 0.0)
        self.assertGreaterEqual(r.max_drawdown_pct(), 0.0)
        self.assertIsInstance(r.sharpe(), float)

    def test_an_empty_result_does_not_divide_by_zero(self):
        r = Result()
        self.assertEqual(r.final(), 0.0)
        self.assertEqual(r.cagr(), 0.0)
        self.assertEqual(r.max_drawdown_pct(), 0.0)
        self.assertEqual(r.sharpe(), 0.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_backtest -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rotation.backtest'`

- [ ] **Step 3: Write minimal implementation**

```python
# rotation/backtest.py
"""
rotation.backtest — walk the rules through history, month by month.

The ordering that keeps it honest: signals are computed from month-end M and
the resulting weights earn month M+1's return. Computing weights and applying
them within the same month would let the strategy act on a close it could not
have seen until that close had happened.

Costs are charged on TURNOVER — the fraction of the book that changes — not
on the whole portfolio every month. A rotation strategy that holds the same
four funds for six months trades nothing in months two through six, and
charging it a full round trip each month would invent a cost that erases the
strategy before any evidence is gathered.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from qd.types import Bar
from rotation.momentum import MonthlySeries, month_ends
from rotation.select import select

MONTHS_PER_YEAR = 12


@dataclass
class Result:
    equity: list[tuple[datetime, float]] = field(default_factory=list)
    weights_by_month: list[tuple[datetime, dict[str, float]]] = field(default_factory=list)
    turnover: list[float] = field(default_factory=list)
    contributed: float = 0.0

    def final(self) -> float:
        return self.equity[-1][1] if self.equity else 0.0

    def cagr(self) -> float:
        """Annualised growth of the equity curve.

        With contributions this is the growth of the account, not a pure
        return on capital; the benchmark receives identical contributions so
        the comparison stays fair.
        """
        if len(self.equity) < 2:
            return 0.0
        start_v, end_v = self.equity[0][1], self.equity[-1][1]
        years = (self.equity[-1][0] - self.equity[0][0]).days / 365.25
        if years <= 0 or start_v <= 0 or end_v <= 0:
            return 0.0
        return (end_v / start_v) ** (1 / years) - 1

    def max_drawdown_pct(self) -> float:
        if not self.equity:
            return 0.0
        peak, worst = self.equity[0][1], 0.0
        for _, v in self.equity:
            peak = max(peak, v)
            if peak > 0:
                worst = max(worst, (peak - v) / peak * 100.0)
        return worst

    def monthly_returns(self) -> list[float]:
        out = []
        for i in range(1, len(self.equity)):
            prev = self.equity[i - 1][1]
            if prev > 0:
                out.append(self.equity[i][1] / prev - 1)
        return out

    def sharpe(self) -> float:
        """Annualised, against a zero risk-free rate.

        Zero rather than T-bills because the comparison that matters here is
        strategy against benchmark, and both are measured the same way.
        """
        rs = self.monthly_returns()
        if len(rs) < 2:
            return 0.0
        sd = statistics.pstdev(rs)
        if sd <= 0:
            return 0.0
        return (statistics.fmean(rs) / sd) * math.sqrt(MONTHS_PER_YEAR)


def _series(bars_by_symbol: dict[str, list[Bar]]) -> dict[str, MonthlySeries]:
    return {s: MonthlySeries(b) for s, b in bars_by_symbol.items()}


def _rebalance_dates(bars_by_symbol, start, end) -> list[datetime]:
    stamps = set()
    for bars in bars_by_symbol.values():
        for b in month_ends(bars):
            if start <= b.end <= end:
                stamps.add(b.end)
    return sorted(stamps)


def run(bars_by_symbol: dict[str, list[Bar]], start: datetime, end: datetime,
        equity: float = 10_000.0, monthly_contribution: float = 0.0,
        cost_bps: float = 10.0) -> Result:
    """Replay the monthly rules between `start` and `end`."""
    series = _series(bars_by_symbol)
    dates = _rebalance_dates(bars_by_symbol, start, end)
    result = Result()
    if not dates:
        return result

    value = equity
    held: dict[str, float] = {}
    result.equity.append((dates[0], value))

    for i in range(len(dates) - 1):
        asof, nxt = dates[i], dates[i + 1]

        # Weights decided from data at `asof`, earning the return to `nxt`.
        target = select(series, asof)
        result.weights_by_month.append((asof, dict(target)))

        moved = sum(abs(target.get(s, 0.0) - held.get(s, 0.0))
                    for s in set(target) | set(held)) / 2.0
        result.turnover.append(moved)
        value *= (1 - moved * cost_bps / 10_000.0)

        growth = 0.0
        for symbol, weight in target.items():
            s = series.get(symbol)
            if s is None:
                growth += weight            # unknown asset holds its value
                continue
            a, b = s.close_on_or_before(asof), s.close_on_or_before(nxt)
            growth += weight * ((b / a) if a and b and a > 0 else 1.0)
        idle = 1.0 - sum(target.values())   # uninvested cash earns nothing
        value = value * (growth + idle)

        value += monthly_contribution
        result.contributed += monthly_contribution
        held = target
        result.equity.append((nxt, value))

    return result


def buy_and_hold(bars: list[Bar], start: datetime, end: datetime,
                 equity: float = 10_000.0, monthly_contribution: float = 0.0
                 ) -> Result:
    """The benchmark: fully invested, contributions added monthly."""
    s = MonthlySeries(bars)
    dates = [b.end for b in s.bars if start <= b.end <= end]
    result = Result()
    if not dates:
        return result

    value = equity
    result.equity.append((dates[0], value))
    for i in range(len(dates) - 1):
        a = s.close_on_or_before(dates[i])
        b = s.close_on_or_before(dates[i + 1])
        if a and b and a > 0:
            value *= b / a
        value += monthly_contribution
        result.contributed += monthly_contribution
        result.equity.append((dates[i + 1], value))
    return result


__all__ = ["run", "buy_and_hold", "Result"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_backtest -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
cd /home/user/quant-desk
git add rotation/backtest.py tests/test_rotation_backtest.py
git commit -m "Add monthly rotation backtest engine"
```

---

### Task 6: Benchmark-relative evaluator and gate

**Files:**
- Create: `rotation/evaluate.py`
- Test: `tests/test_rotation_evaluate.py`

**Interfaces:**
- Consumes: `rotation.backtest.run`, `rotation.backtest.buy_and_hold`, `rotation.backtest.Result`, `rotation.archive.BENCHMARK`.
- Produces:
  - `Fold` dataclass: `label: str`, `strategy: Result`, `benchmark: Result`, with `beat_return() -> bool` and `beat_drawdown() -> bool`.
  - `Evaluation` dataclass: `folds: list[Fold]`, `full_strategy: Result`, `full_benchmark: Result`, `cost_sweep: dict[float, Result]`, `verdict: str`, `reasons: list[str]`, with `report() -> str`.
  - `evaluate(bars_by_symbol, benchmark_bars, start, end, folds=4, equity=10_000.0, monthly_contribution=0.0) -> Evaluation`
  - `MIN_FOLDS_BEATEN: int = 3`, `DRAWDOWN_RATIO: float = 0.75`

**Background — the standard, from the spec:** claim A is beating SPY on CAGR;
claim B is a higher Sharpe *and* a maximum drawdown no worse than 0.75× SPY's.
The verdict is `OUTPERFORMS` only if claim A holds in at least 3 of 4 folds and
survives the 2× cost run. `NO EDGE` is a successful outcome, not a failure.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_evaluate.py
"""
The gate.

It must be able to say no. The previous strategy in this repo passed three
successive evaluations that were all wrong, and each was caught by a number
that did not reconcile — so the gate is written to reject by default and the
tests below check the rejections, not just the acceptance.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.types import UTC, Bar
from rotation.backtest import Result
from rotation.evaluate import (
    DRAWDOWN_RATIO, MIN_FOLDS_BEATEN, Evaluation, Fold, evaluate,
)


def curve(values, start=datetime(2016, 1, 31, tzinfo=UTC)):
    r = Result()
    r.equity = [(start + timedelta(days=30 * i), v) for i, v in enumerate(values)]
    return r


class FoldTest(unittest.TestCase):
    def test_beating_the_benchmark_on_return(self):
        f = Fold("a", curve([100, 200]), curve([100, 150]))
        self.assertTrue(f.beat_return())

    def test_losing_to_the_benchmark_on_return(self):
        f = Fold("a", curve([100, 110]), curve([100, 150]))
        self.assertFalse(f.beat_return())

    def test_drawdown_must_be_at_most_three_quarters_of_the_benchmark(self):
        # strategy falls 10%, benchmark falls 50% -> passes
        good = Fold("a", curve([100, 90, 120]), curve([100, 50, 120]))
        self.assertTrue(good.beat_drawdown())
        # strategy falls 48%, benchmark falls 50% -> fails (0.96 > 0.75)
        bad = Fold("a", curve([100, 52, 120]), curve([100, 50, 120]))
        self.assertFalse(bad.beat_drawdown())


def flat_bars(symbol, months, growth):
    bars, c = [], 100.0
    base = datetime(2015, 1, 31, tzinfo=UTC)
    for m in range(months):
        end = base + timedelta(days=30 * m)
        bars.append(Bar(symbol=symbol, start=end - timedelta(days=1), end=end,
                        open=c, high=c, low=c, close=c, volume=1.0))
        c *= (1 + growth)
    return bars


class EvaluateTest(unittest.TestCase):
    START = datetime(2016, 6, 1, tzinfo=UTC)
    END = datetime(2021, 1, 1, tzinfo=UTC)

    def losing_universe(self):
        """Sectors that badly trail the benchmark."""
        return {s: flat_bars(s, 80, 0.001) for s in
                ("XLK", "XLF", "XLV", "XLE", "IEF", "SHY")}

    def test_a_strategy_that_trails_the_benchmark_gets_no_edge(self):
        ev = evaluate(self.losing_universe(), flat_bars("SPY", 80, 0.02),
                      self.START, self.END, folds=4, equity=1000.0)
        self.assertEqual(ev.verdict, "NO EDGE")
        self.assertTrue(ev.reasons)

    def test_no_data_is_insufficient_data_not_a_verdict(self):
        ev = evaluate({}, [], self.START, self.END, folds=4, equity=1000.0)
        self.assertEqual(ev.verdict, "INSUFFICIENT DATA")

    def test_the_report_names_the_verdict_and_the_folds(self):
        ev = evaluate(self.losing_universe(), flat_bars("SPY", 80, 0.02),
                      self.START, self.END, folds=4, equity=1000.0)
        text = ev.report()
        self.assertIn(ev.verdict, text)
        self.assertIn("fold", text.lower())
        self.assertIn("SPY", text)

    def test_the_thresholds_match_the_spec(self):
        self.assertEqual(MIN_FOLDS_BEATEN, 3)
        self.assertAlmostEqual(DRAWDOWN_RATIO, 0.75)

    def test_a_cost_sweep_is_run(self):
        ev = evaluate(self.losing_universe(), flat_bars("SPY", 80, 0.02),
                      self.START, self.END, folds=4, equity=1000.0)
        self.assertEqual(set(ev.cost_sweep), {1.0, 1.5, 2.0})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_evaluate -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rotation.evaluate'`

- [ ] **Step 3: Write minimal implementation**

```python
# rotation/evaluate.py
"""
rotation.evaluate — judge the strategy against buy-and-hold, and say no.

The benchmark is SPY with identical contributions, never zero. A strategy that
merely makes money has proved nothing: the alternative was never "cash", it
was "buy the index and do nothing".

Two claims, registered separately in the spec because they are expected to
disagree:

  A  raw return   CAGR above SPY's, net of costs. The stated goal, and the
                  harder bar — momentum rotation typically trails in long
                  bull markets and earns its keep in crashes.
  B  risk-adjust  Higher Sharpe AND maximum drawdown no worse than 0.75x
                  SPY's.

The verdict is OUTPERFORMS only when claim A holds in at least three of four
folds and survives the 2x cost run. NO EDGE is a successful outcome.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from qd.types import Bar
from rotation.backtest import Result, buy_and_hold, run

logger = logging.getLogger(__name__)

MIN_FOLDS_BEATEN = 3
DRAWDOWN_RATIO = 0.75
COST_MULTS: tuple[float, ...] = (1.0, 1.5, 2.0)
STRESS_MULT = 2.0
BASE_COST_BPS = 10.0


@dataclass
class Fold:
    label: str
    strategy: Result
    benchmark: Result

    def beat_return(self) -> bool:
        return self.strategy.cagr() > self.benchmark.cagr()

    def beat_drawdown(self) -> bool:
        bench = self.benchmark.max_drawdown_pct()
        if bench <= 0:
            return self.strategy.max_drawdown_pct() <= 0
        return self.strategy.max_drawdown_pct() <= bench * DRAWDOWN_RATIO


@dataclass
class Evaluation:
    folds: list[Fold] = field(default_factory=list)
    full_strategy: Result = field(default_factory=Result)
    full_benchmark: Result = field(default_factory=Result)
    cost_sweep: dict[float, Result] = field(default_factory=dict)
    verdict: str = "INSUFFICIENT DATA"
    reasons: list[str] = field(default_factory=list)

    def report(self) -> str:
        s, b = self.full_strategy, self.full_benchmark
        lines = [
            "=" * 68,
            f"  ROTATION EVALUATION: {self.verdict}",
            "=" * 68,
            "",
            f"  {'':<16}{'strategy':>12}{'SPY':>12}",
            f"  {'CAGR':<16}{s.cagr():>11.2%}{b.cagr():>12.2%}",
            f"  {'max drawdown':<16}{s.max_drawdown_pct():>11.1f}%"
            f"{b.max_drawdown_pct():>11.1f}%",
            f"  {'Sharpe':<16}{s.sharpe():>12.2f}{b.sharpe():>12.2f}",
            f"  {'final':<16}{s.final():>12,.0f}{b.final():>12,.0f}",
            "",
            "  Cost stress (strategy CAGR)",
        ]
        for mult, res in sorted(self.cost_sweep.items()):
            lines.append(f"    {mult:>4.1f}x  {res.cagr():>8.2%}")
        lines += ["", "  Folds"]
        for f in self.folds:
            mark = "beat" if f.beat_return() else "lost"
            lines.append(
                f"    {f.label}  strategy {f.strategy.cagr():>7.2%}  "
                f"SPY {f.benchmark.cagr():>7.2%}  {mark}  "
                f"maxDD {f.strategy.max_drawdown_pct():>5.1f}% vs "
                f"{f.benchmark.max_drawdown_pct():>5.1f}%"
            )
        if self.reasons:
            lines += ["", "  Verdict reasoning"]
            lines += [f"    - {r}" for r in self.reasons]
        lines.append("=" * 68)
        return "\n".join(lines)


def evaluate(bars_by_symbol: dict[str, list[Bar]], benchmark_bars: list[Bar],
             start: datetime, end: datetime, folds: int = 4,
             equity: float = 10_000.0, monthly_contribution: float = 0.0
             ) -> Evaluation:
    """Full evaluation: folds, cost sweep, verdict."""
    ev = Evaluation()
    if not bars_by_symbol or not benchmark_bars:
        ev.reasons.append("no data")
        return ev

    ev.full_strategy = run(bars_by_symbol, start, end, equity=equity,
                           monthly_contribution=monthly_contribution,
                           cost_bps=BASE_COST_BPS)
    ev.full_benchmark = buy_and_hold(benchmark_bars, start, end, equity=equity,
                                     monthly_contribution=monthly_contribution)
    if len(ev.full_strategy.equity) < 2:
        ev.reasons.append("not enough months to evaluate")
        return ev

    for mult in COST_MULTS:
        ev.cost_sweep[mult] = run(bars_by_symbol, start, end, equity=equity,
                                  monthly_contribution=monthly_contribution,
                                  cost_bps=BASE_COST_BPS * mult)

    span = (end - start) / folds
    for i in range(folds):
        f_start, f_end = start + span * i, start + span * (i + 1)
        ev.folds.append(Fold(
            label=f"{f_start:%Y-%m}..{f_end:%Y-%m}",
            strategy=run(bars_by_symbol, f_start, f_end, equity=equity,
                         monthly_contribution=monthly_contribution,
                         cost_bps=BASE_COST_BPS),
            benchmark=buy_and_hold(benchmark_bars, f_start, f_end,
                                   equity=equity,
                                   monthly_contribution=monthly_contribution),
        ))

    beaten = sum(1 for f in ev.folds if f.beat_return())
    stressed = ev.cost_sweep.get(STRESS_MULT)

    if beaten < MIN_FOLDS_BEATEN:
        ev.reasons.append(
            f"beat SPY in only {beaten}/{len(ev.folds)} folds, need "
            f"{MIN_FOLDS_BEATEN} — the result lives in specific periods")
    if stressed is not None and stressed.cagr() <= ev.full_benchmark.cagr():
        ev.reasons.append(
            f"at {STRESS_MULT}x costs CAGR is {stressed.cagr():.2%} vs SPY's "
            f"{ev.full_benchmark.cagr():.2%} — the edge is inside the costs")
    if ev.full_strategy.cagr() <= ev.full_benchmark.cagr():
        ev.reasons.append(
            f"CAGR {ev.full_strategy.cagr():.2%} does not beat SPY's "
            f"{ev.full_benchmark.cagr():.2%}")

    ev.verdict = "NO EDGE" if ev.reasons else "OUTPERFORMS"
    if not ev.reasons:
        ev.reasons.append(
            f"beat SPY in {beaten}/{len(ev.folds)} folds and survived "
            f"{STRESS_MULT}x costs")
    return ev


__all__ = ["evaluate", "Evaluation", "Fold", "MIN_FOLDS_BEATEN",
           "DRAWDOWN_RATIO", "COST_MULTS", "STRESS_MULT", "BASE_COST_BPS"]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_evaluate -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
cd /home/user/quant-desk
git add rotation/evaluate.py tests/test_rotation_evaluate.py
git commit -m "Add benchmark-relative rotation evaluator and gate"
```

---

### Task 7: Falsification checks

**Files:**
- Create: `rotation/falsify.py`
- Test: `tests/test_rotation_falsify.py`

**Interfaces:**
- Consumes: `rotation.backtest.run`, `rotation.backtest.Result`, `rotation.momentum.MonthlySeries`.
- Produces:
  - `lookback_structure(bars_by_symbol, benchmark_bars, start, end, equity=10_000.0) -> dict[str, float]` — CAGR under 1-month, 3-month, 6-month and 12-month lookbacks.
  - `momentum_is_real(results: dict[str, float]) -> bool` — True when 12-month beats 1-month.
  - `beat_every_fold(ev) -> bool` — True when the strategy beat SPY in every fold, which the spec registers as evidence of a bug.

**Background — from spec §8.** Three registered predictions. A 1-month
lookback historically *reverses*; if it performs as well as 12-month the
result is noise wearing a momentum label. And beating SPY in *every* fold
including a decade-long bull market is a red flag rather than a triumph.

Implementing `lookback_structure` requires running the strategy with a
non-default lookback. Add an optional `lookback` parameter to
`rotation.momentum.twelve_month_return` via a new function
`horizon_return(series, asof, months)` and have `select` accept an optional
`months: int = 12`; `run` passes it through. Keep the default at 12 so no
existing behaviour changes.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_falsify.py
"""
The registered falsification checks from spec section 8.

These are not diagnostics. They are the conditions, written before the test,
under which the hypothesis is wrong — and the third one is the most valuable
thing in the file: a strategy that beats the benchmark in EVERY period,
including a decade-long bull market, has a bug rather than an edge.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from qd.types import UTC, Bar
from rotation.backtest import Result
from rotation.evaluate import Evaluation, Fold
from rotation.falsify import beat_every_fold, momentum_is_real


def curve(values, start=datetime(2016, 1, 31, tzinfo=UTC)):
    r = Result()
    r.equity = [(start + timedelta(days=30 * i), v) for i, v in enumerate(values)]
    return r


class MomentumIsRealTest(unittest.TestCase):
    def test_twelve_month_beating_one_month_is_momentum(self):
        self.assertTrue(momentum_is_real({"1m": 0.02, "12m": 0.11}))

    def test_one_month_doing_just_as_well_is_noise(self):
        """Short-horizon returns historically REVERSE. If a 1-month lookback
        works as well as 12-month, whatever was measured is not momentum."""
        self.assertFalse(momentum_is_real({"1m": 0.12, "12m": 0.11}))

    def test_missing_keys_are_not_a_pass(self):
        self.assertFalse(momentum_is_real({"12m": 0.11}))


class BeatEveryFoldTest(unittest.TestCase):
    def evaluation(self, pairs):
        ev = Evaluation()
        ev.folds = [Fold(f"f{i}", curve(s), curve(b))
                    for i, (s, b) in enumerate(pairs)]
        return ev

    def test_beating_every_fold_is_flagged(self):
        ev = self.evaluation([([100, 200], [100, 150])] * 4)
        self.assertTrue(beat_every_fold(ev))

    def test_losing_one_fold_is_normal(self):
        ev = self.evaluation([([100, 200], [100, 150])] * 3
                             + [([100, 110], [100, 150])])
        self.assertFalse(beat_every_fold(ev))

    def test_no_folds_is_not_a_clean_sweep(self):
        self.assertFalse(beat_every_fold(Evaluation()))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_falsify -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rotation.falsify'`

- [ ] **Step 3: Add the lookback parameter, then write the module**

First extend `rotation/momentum.py` — add below `twelve_month_return`:

```python
def horizon_return(series: MonthlySeries, asof: datetime,
                   months: int = 12) -> Optional[float]:
    """Total return over `months`, ending at `asof`. None when unavailable.

    Exists for the falsification check in spec section 8: momentum should be
    materially stronger at 6-12 months than at 1 month, which historically
    reverses. It is NOT a tuning knob — the strategy's lookback is frozen at
    twelve months.
    """
    window = timedelta(days=int(30.44 * months))
    if series.first_date is None or asof - series.first_date < window:
        return None
    now = series.close_on_or_before(asof)
    then = series.close_on_or_before(asof - window)
    if not now or not then or then <= 0:
        return None
    return now / then - 1.0
```

Add `"horizon_return"` to that module's `__all__`.

Then in `rotation/select.py`, thread the horizon through — replace the bodies of
`rank`, `_defensive_pick` and `select` signatures to accept `months: int = 12`
and call `horizon_return(series, asof, months)` instead of
`twelve_month_return(series, asof)`:

```python
from rotation.momentum import MonthlySeries, horizon_return


def rank(series_by_symbol: dict[str, MonthlySeries], asof: datetime,
         months: int = 12) -> list[Ranking]:
    rows: list[Ranking] = []
    for symbol in sorted(series_by_symbol):
        r = horizon_return(series_by_symbol[symbol], asof, months)
        if r is not None:
            rows.append(Ranking(symbol, r))
    rows.sort(key=lambda x: (-x.ret, x.symbol))
    return rows


def _defensive_pick(series_by_symbol, asof, months: int = 12) -> Optional[str]:
    for symbol in DEFENSIVE:
        series = series_by_symbol.get(symbol)
        if series is None:
            continue
        r = horizon_return(series, asof, months)
        if r is not None and r > 0:
            return symbol
    return DEFENSIVE[-1] if DEFENSIVE[-1] in series_by_symbol else None


def select(series_by_symbol: dict[str, MonthlySeries], asof: datetime,
           months: int = 12) -> dict[str, float]:
    ranked = rank(series_by_symbol, asof, months)
    weights: dict[str, float] = {}
    fallback_slots = 0
    for row in ranked[:TOP_N]:
        if row.ret > 0:
            weights[row.symbol] = weights.get(row.symbol, 0.0) + WEIGHT
        else:
            fallback_slots += 1
    fallback_slots += max(0, TOP_N - len(ranked))
    if fallback_slots:
        pick = _defensive_pick(series_by_symbol, asof, months)
        if pick:
            weights[pick] = weights.get(pick, 0.0) + WEIGHT * fallback_slots
    return weights
```

In `rotation/backtest.py`, add `lookback_months: int = 12` to `run`'s signature
and change the select call to `select(series, asof, lookback_months)`.

Now the new module:

```python
# rotation/falsify.py
"""
rotation.falsify — the registered falsification checks from spec section 8.

Written before any result exists, so a disappointing outcome cannot be
reinterpreted afterwards and a flattering one cannot be trusted uncritically.
"""

from __future__ import annotations

from datetime import datetime

from qd.types import Bar
from rotation.backtest import run

HORIZONS: tuple[int, ...] = (1, 3, 6, 12)


def lookback_structure(bars_by_symbol: dict[str, list[Bar]],
                       benchmark_bars: list[Bar], start: datetime,
                       end: datetime, equity: float = 10_000.0
                       ) -> dict[str, float]:
    """CAGR under each lookback horizon.

    Momentum should be materially stronger at 6-12 months than at 1 month,
    which historically reverses. If the 1-month version does just as well,
    the effect measured is not momentum.
    """
    out: dict[str, float] = {}
    for months in HORIZONS:
        res = run(bars_by_symbol, start, end, equity=equity,
                  lookback_months=months)
        out[f"{months}m"] = res.cagr()
    return out


def momentum_is_real(results: dict[str, float]) -> bool:
    """True when the 12-month lookback beats the 1-month one."""
    if "1m" not in results or "12m" not in results:
        return False
    return results["12m"] > results["1m"]


def beat_every_fold(ev) -> bool:
    """True when the strategy beat the benchmark in EVERY fold.

    Registered in the spec as evidence of a BUG, not an edge. Momentum
    rotation is supposed to trail during long bull markets; a clean sweep
    across a sample containing 2010-2021 means look-ahead somewhere.
    """
    folds = getattr(ev, "folds", [])
    return bool(folds) and all(f.beat_return() for f in folds)


__all__ = ["lookback_structure", "momentum_is_real", "beat_every_fold",
           "HORIZONS"]
```

- [ ] **Step 4: Run the full suite to verify nothing regressed**

Run: `cd /home/user/quant-desk && python3 -m unittest discover -s tests -q`
Expected: all tests pass, including the 331 pre-existing ones

- [ ] **Step 5: Commit**

```bash
cd /home/user/quant-desk
git add rotation/falsify.py rotation/momentum.py rotation/select.py \
        rotation/backtest.py tests/test_rotation_falsify.py
git commit -m "Add registered falsification checks and lookback parameter"
```

---

### Task 8: CLI and the real evaluation

**Files:**
- Create: `rotation/cli.py`
- Test: `tests/test_rotation_cli.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `main(argv=None) -> int` with subcommands `fetch` and `evaluate`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rotation_cli.py
"""The CLI wiring. Thin by design — the logic is tested in its own modules."""

from __future__ import annotations

import unittest

from rotation.cli import build_parser


class ParserTest(unittest.TestCase):
    def test_fetch_takes_an_output_directory(self):
        args = build_parser().parse_args(["fetch", "--out", "data/rotation"])
        self.assertEqual(args.out, "data/rotation")

    def test_evaluate_takes_an_archive_and_folds(self):
        args = build_parser().parse_args(
            ["evaluate", "--archive", "data/rotation", "--folds", "5"])
        self.assertEqual(args.archive, "data/rotation")
        self.assertEqual(args.folds, 5)

    def test_evaluate_defaults_to_four_folds(self):
        args = build_parser().parse_args(["evaluate"])
        self.assertEqual(args.folds, 4)

    def test_evaluate_accepts_a_monthly_contribution(self):
        args = build_parser().parse_args(["evaluate", "--contribution", "300"])
        self.assertAlmostEqual(args.contribution, 300.0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_cli -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'rotation.cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# rotation/cli.py
"""rotation.cli — fetch the archive, run the evaluation."""

from __future__ import annotations

import argparse
import logging
from datetime import datetime

from qd.types import UTC
from rotation.archive import BENCHMARK, build, load
from rotation.evaluate import evaluate
from rotation.falsify import lookback_structure, momentum_is_real, beat_every_fold


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="rotation")
    sub = p.add_subparsers(dest="cmd", required=True)

    f = sub.add_parser("fetch", help="download the long-history archive")
    f.add_argument("--out", default="data/rotation")

    e = sub.add_parser("evaluate", help="run the full evaluation")
    e.add_argument("--archive", default="data/rotation")
    e.add_argument("--folds", type=int, default=4)
    e.add_argument("--equity", type=float, default=10_000.0)
    e.add_argument("--contribution", type=float, default=0.0)
    return p


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args(argv)

    if args.cmd == "fetch":
        counts = build(args.out)
        total = sum(counts.values())
        print(f"{total:,} bars across {len(counts)} symbols -> {args.out}")
        return 0

    bars = load(args.archive)
    benchmark = bars.pop(BENCHMARK, [])
    if not bars or not benchmark:
        print(f"no archive at {args.archive} — run `fetch` first")
        return 1

    starts = [b[0].end for b in bars.values() if b]
    ends = [b[-1].end for b in bars.values() if b]
    start, end = min(starts), max(ends)

    ev = evaluate(bars, benchmark, start, end, folds=args.folds,
                  equity=args.equity, monthly_contribution=args.contribution)
    print(ev.report())

    print("\n  Falsification checks (spec section 8)")
    structure = lookback_structure(bars, benchmark, start, end, equity=args.equity)
    for k, v in structure.items():
        print(f"    lookback {k:>3}: CAGR {v:>7.2%}")
    print(f"    12m beats 1m (momentum is real): {momentum_is_real(structure)}")
    swept = beat_every_fold(ev)
    print(f"    beat SPY in EVERY fold: {swept}"
          + ("   <-- INVESTIGATE: this signals a bug, not an edge" if swept else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /home/user/quant-desk && python3 -m unittest tests.test_rotation_cli -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Run the real evaluation**

Run:
```bash
cd /home/user/quant-desk && python3 -m rotation.cli evaluate \
    --archive data/rotation --folds 4 --equity 10000 --contribution 300
```
Expected: a full report. **Do not tune anything in response to the numbers.**
Record the result and report it as-is. If `beat SPY in EVERY fold` is True,
stop and investigate for look-ahead before believing any of it.

- [ ] **Step 6: Run the whole suite and commit**

```bash
cd /home/user/quant-desk
python3 -m unittest discover -s tests -q
git add rotation/cli.py tests/test_rotation_cli.py
git commit -m "Add rotation CLI and run the first evaluation"
```

---

### Task 9: Record the measured result

**Files:**
- Create: `rotation/HYPOTHESIS.md`

**Interfaces:**
- Consumes: the output of Task 8, Step 5.
- Produces: a committed record of the registered claims and the measured outcome.

- [ ] **Step 1: Write the hypothesis record**

Create `rotation/HYPOTHESIS.md` containing, in this order:

1. The hypothesis exactly as written in spec section 1, including the honest
   statement that clause 3 is weaker here than for PEAD.
2. The two registered claims from spec section 2, with the note that claim A
   was the stated goal and claim B the likely outcome.
3. The frozen parameters from spec section 4 and the rule that changing them
   requires a new spec.
4. **The measured result** — paste the full report from Task 8, Step 5
   verbatim, including the fold table and cost sweep.
5. **The falsification checks and their outcomes**, verbatim.
6. A short honest reading of what the result is evidence about: broad-sector
   momentum on US-listed ETFs over the tested window, net of a 10bp cost
   model — and explicitly not evidence about thematic ETFs, individual
   stocks, or the LSE UCITS funds that would actually be traded.

- [ ] **Step 2: Commit**

```bash
cd /home/user/quant-desk
git add rotation/HYPOTHESIS.md
git commit -m "Record the measured sector rotation result"
```

---

## Self-Review

**1. Spec coverage**

| Spec section | Task |
|---|---|
| §1 hypothesis | 9 |
| §2 success criteria (claims A/B, 0.75× drawdown) | 6 |
| §3 universe, point-in-time membership | 2 (`UNIVERSE`), 3 (`None` for short history) |
| §4 rules, frozen parameters, tie-break | 4 |
| §5 risk limits (long only, no leverage, equal weight, no stops) | 4, 5 |
| §6 data, 20+ years, adjusted close, `known_at` | 1, 2 |
| §7 evaluation, folds, cost stress, benchmark-relative | 6 |
| §8 falsification | 7, 8 |
| §9 architecture, reuse boundaries | all — `qd.types`/`qd.clock` only |
| §10 honest expected outcome | 9 |

No gaps.

**2. Placeholder scan** — no TBDs, no "handle errors appropriately", no
"similar to Task N". Every code step contains runnable code.

**3. Type consistency** — `MonthlySeries`, `Ranking`, `Result`, `Fold`,
`Evaluation` are defined once and used with consistent signatures.
`select(series, asof, months=12)` gains its third parameter in Task 7 with a
default, so Task 5's two-argument calls remain valid. `run(...)` gains
`lookback_months=12` the same way. `Result.cagr()`, `.max_drawdown_pct()`,
`.sharpe()`, `.final()` are used identically in Tasks 5, 6 and 7.

**One deliberate deviation from the spec**, recorded here rather than hidden:
spec §7 lists `OUTPERFORMS` / `NO EDGE` / `INSUFFICIENT DATA` as verdicts and
describes claim B as a registered claim. The gate in Task 6 decides the verdict
on claim A only; claim B is computed and reported per fold via
`Fold.beat_drawdown()` but does not change the verdict. This keeps the verdict
one-dimensional and honest — a strategy that loses on return but wins on
drawdown gets `NO EDGE` with the drawdown numbers visible, rather than a
verdict that quietly redefines success. If you would rather claim B could
produce its own verdict, say so and I will amend both documents.
