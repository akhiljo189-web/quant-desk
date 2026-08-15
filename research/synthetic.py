"""
research.synthetic — deterministic fake market data.

Two uses, and it is important to be clear that only the second is research.

  1. Exercising the machinery offline. The full pipeline — providers, engine,
     risk, broker, evaluation — runs with no API keys and no network, which
     makes the test suite meaningful and the first run of the system possible
     before paying a data vendor.

  2. A NULL TEST, which is the valuable one. This generator produces price paths
     with no predictable structure: the returns are random, and the news,
     earnings and options events are sprinkled independently of what price
     subsequently does. There is nothing to find. So the evaluator must return
     NO EDGE on this data, and `tests/test_null_hypothesis.py` asserts it.

The null test is the single most important test in the repository. A research
harness that reports an edge on random data is not a harness, it is a random
number generator with a confident interface — and every plausible-looking
backtest it later produces on real data is uninterpretable, because you have no
evidence the tool can tell the difference.

Deliberately absent: any mechanism that makes news predict returns, or flow lead
price. Adding one to "check the system can find signal" would be building a
detector for a phenomenon that was inserted for the detector to find.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Iterable, Optional, Sequence

from qd.clock import CALENDAR, ET, MarketCalendar
from qd.providers.replay import ReplayDataset
from qd.types import (
    Bar, EarningsEvent, NewsItem, OptionContract, OptionTrade, Right, UTC,
)

HEADLINES = [
    ("{sym} announces $2B share repurchase program", "business wire"),
    ("{sym} raises FY guidance above consensus", "reuters"),
    ("{sym} cuts Q3 outlook on weak demand", "reuters"),
    ("{sym} prices $500M common stock offering", "globe newswire"),
    ("Analysts upgrade {sym} to buy on margin outlook", "zacks"),
    ("{sym} downgraded to underweight at a major bank", "cnbc"),
    ("{sym} wins $1.2B government contract", "pr newswire"),
    ("{sym} names new CFO after abrupt departure", "bloomberg"),
    ("{sym} faces SEC probe over revenue recognition", "dow jones"),
    ("{sym} partners with a major cloud provider", "business wire"),
]


@dataclass
class SyntheticSpec:
    symbols: Sequence[str]
    start: date
    end: date
    seed: int = 42
    start_price: float = 100.0
    # Calibrated to the MID-CAP universe the config now describes, at the
    # HOURLY interval the system evaluates on. 0.35 annual vol on 5-minute
    # bars produced ~0.4% bar-ATR against a 0.8% floor: every synthetic
    # replay was rejected by is_tradeable before its first assessment, and
    # the null-hypothesis test spent months proving that zero trades is not
    # an edge — vacuously true and testing nothing.
    annual_vol: float = 0.70
    bar_minutes: int = 60
    news_per_day: float = 0.4          # expected headlines per symbol per day
    earnings_every_days: int = 90
    option_trades_per_day: int = 40
    drift: float = 0.0                 # zero: no free directional edge
    # The regime layer needs the market index AND 60 daily bars before it will
    # classify anything; without both it returns UNKNOWN and the strategy
    # refuses every candidate. Generating neither is why synthetic replays
    # produced zero trades for months while the null-hypothesis test passed
    # by asserting that nothing has no edge.
    market_symbol: str = "SPY"


def _session_times(d: date, minutes: int, cal: MarketCalendar) -> list[tuple[datetime, datetime]]:
    bounds = cal.session_bounds(d)
    if bounds is None:
        return []
    open_, close_ = bounds
    out = []
    t = open_
    step = timedelta(minutes=minutes)
    while t + step <= close_:
        out.append((t, t + step))
        t += step
    return out


def generate(spec: SyntheticSpec, cal: MarketCalendar = CALENDAR) -> ReplayDataset:
    """Build a complete dataset. Same seed gives byte-identical output."""
    rng = random.Random(spec.seed)
    ds = ReplayDataset()
    days = cal.trading_days_between(spec.start, spec.end)

    # The market index is generated too. The regime layer classifies against
    # it, and a missing index means MarketContext.market is permanently
    # UNKNOWN, which blocks every entry regardless of signal.
    names = list(spec.symbols)
    if spec.market_symbol and spec.market_symbol not in names:
        names.append(spec.market_symbol)

    for si, sym in enumerate(names):
        price = spec.start_price * (1.0 + 0.1 * si)
        bars_per_day = max(1, int(390 / spec.bar_minutes))
        per_bar_vol = spec.annual_vol / math.sqrt(252 * bars_per_day)
        base_volume = 200_000 + si * 50_000

        for d in days:
            slots = _session_times(d, spec.bar_minutes, cal)
            if not slots:
                continue
            day_open = price
            day_high, day_low = price, price
            total_vol = 0.0

            for i, (bs, be) in enumerate(slots):
                # U-shaped intraday volume: heavy at the open and into the
                # close, thin at lunch. Real enough to exercise RVOL.
                frac = i / max(1, len(slots) - 1)
                shape = 1.6 - 1.4 * math.sin(math.pi * frac) + 0.6 * frac
                vol = max(1000.0, rng.gauss(base_volume * shape / len(slots),
                                            base_volume * 0.15 / len(slots)))

                ret = rng.gauss(spec.drift / (252 * bars_per_day), per_bar_vol)
                o = price
                c = max(0.5, o * (1.0 + ret))
                wick = abs(rng.gauss(0, per_bar_vol * 0.6)) * o
                h = max(o, c) + wick
                l = max(0.1, min(o, c) - wick)

                ds.add_bars(sym, [Bar(
                    symbol=sym, start=bs, end=be, open=o, high=h, low=l,
                    close=c, volume=vol, vwap=(h + l + c) / 3,
                )])
                price = c
                day_high, day_low = max(day_high, h), min(day_low, l)
                total_vol += vol

            session_close = datetime.combine(d, time(16, 0), tzinfo=ET).astimezone(UTC)
            ds.add_daily(sym, [Bar(
                symbol=sym,
                start=datetime.combine(d, time(9, 30), tzinfo=ET).astimezone(UTC),
                end=session_close,      # a daily bar is known at its CLOSE
                open=day_open, high=day_high, low=day_low, close=price,
                volume=total_vol, vwap=(day_high + day_low + price) / 3,
            )])

            # Overnight gap, so daily and intraday do not join up implausibly.
            price *= 1.0 + rng.gauss(0, per_bar_vol * 3)

            # Headlines, placed independently of what price then does.
            if rng.random() < spec.news_per_day:
                tmpl, source = rng.choice(HEADLINES)
                slot = rng.choice(slots)
                published = slot[0] + timedelta(seconds=rng.randint(0, 280))
                ds.add_news([NewsItem(
                    id=f"{sym}-{d}-{rng.randint(1000, 9999)}",
                    symbols=(sym,),
                    headline=tmpl.format(sym=sym),
                    published_at=published,
                    received_at=published + timedelta(seconds=rng.uniform(5, 90)),
                    source=source,
                )])

            # Options tape.
            for _ in range(rng.randint(0, spec.option_trades_per_day)):
                slot = rng.choice(slots)
                ts = slot[0] + timedelta(seconds=rng.randint(0, 290))
                right = Right.CALL if rng.random() < 0.55 else Right.PUT
                dte = rng.choice([3, 10, 17, 24, 45])
                strike = round(price * (1 + rng.gauss(0, 0.04)), 0)
                mid = max(0.15, abs(rng.gauss(2.5, 1.2)))
                spread = mid * rng.uniform(0.02, 0.10)
                bid, ask = mid - spread / 2, mid + spread / 2
                # Roughly a third lift the offer, a third hit the bid, a third
                # print inside — close enough to a real tape's shape.
                roll = rng.random()
                px = ask if roll < 0.34 else (bid if roll < 0.68 else mid)
                ds.add_option_trades(sym, [OptionTrade(
                    contract=OptionContract(
                        underlying=sym,
                        expiry=datetime.combine(
                            d + timedelta(days=dte), time(16, 0), tzinfo=ET
                        ).astimezone(UTC),
                        strike=strike, right=right,
                        occ_symbol=f"O:{sym}{d:%y%m%d}{right.value}{int(strike*1000):08d}",
                    ),
                    ts=ts, price=px, size=rng.randint(5, 400),
                    exchange=rng.choice(["CBOE", "ISE", "PHLX", "BOX", "AMEX"]),
                    nbbo_bid=bid, nbbo_ask=ask,
                    underlying_price=price,
                    open_interest=rng.randint(50, 5000),
                    received_at=ts + timedelta(milliseconds=250),
                )])

        # Earnings on a fixed cadence, with actuals released after the close.
        for n, d in enumerate(days):
            if n % spec.earnings_every_days != spec.earnings_every_days - 1:
                continue
            release = datetime.combine(d, time(16, 15), tzinfo=ET).astimezone(UTC)
            est = round(rng.uniform(0.8, 2.5), 2)
            ds.add_earnings([EarningsEvent(
                symbol=sym,
                report_date=datetime.combine(d, time(0, 0), tzinfo=UTC),
                session="amc",
                # The schedule is public well in advance; the numbers are not.
                scheduled_known_at=release - timedelta(days=21),
                eps_estimate=est,
                # Big enough that some surprises clear the trigger floor.
                # The null property lives in the PRICES being independent of
                # the events, not in the events being too small to act on —
                # events nobody trades test nothing.
                eps_actual=round(est * (1 + rng.gauss(0, 0.35)), 2),
                revenue_estimate=1_000_000_000.0,
                revenue_actual=1_000_000_000.0 * (1 + rng.gauss(0, 0.03)),
                released_at=release,
                fiscal_period=f"Q{(n // spec.earnings_every_days) % 4 + 1}",
            )])

    ds.freeze()
    return ds


def small_dataset(seed: int = 7) -> ReplayDataset:
    """A compact dataset for the test suite."""
    return generate(SyntheticSpec(
        symbols=("AAPL", "MSFT", "NVDA"),
        start=date(2026, 3, 2), end=date(2026, 3, 27), seed=seed,
        # Frequent enough that a short window still exercises the earnings
        # blackout and PEAD paths.
        earnings_every_days=10,
    ))


__all__ = ["SyntheticSpec", "generate", "small_dataset"]
