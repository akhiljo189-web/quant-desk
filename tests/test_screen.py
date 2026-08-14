"""
Point-in-time universe selection.

Every test here corresponds to a way the first run of this screen produced a
universe that looked entirely reasonable and was wrong:

  the split trap      split-adjusted prices multiplied by as-filed share
                      counts put Booking Holdings, a $100B company, in the
                      mid-cap band at $4B
  the fund trap       an oil ETF files with the SEC and reports units
                      outstanding, so units x price screened in as a mid-cap
  the turnover trap   "most liquid name in each stratum" selected GameStop,
                      Virgin Galactic and Beyond Meat — the most contested
                      stories of the year, in a strategy whose whole premise
                      is that repricing is slow
  the future trap     a share count filed after the selection date
"""

from __future__ import annotations

import unittest
from datetime import date

from research.screen import (
    Candidate, _frames, dollar_volume, shares_outstanding, stratified,
)


def cand(symbol: str, cap: float, adv: float) -> Candidate:
    return Candidate(symbol=symbol, cik="0000000001", name=symbol, price=50.0,
                     adv=adv, shares=cap / 50.0, market_cap=cap,
                     shares_dated=date(2021, 12, 31))


class StubHttp:
    def __init__(self, payloads):
        self.payloads = payloads
        self.calls = []

    def get(self, path, params=None, tag=None):
        self.calls.append((path, dict(params or {})))
        for key, value in self.payloads.items():
            if key in path:
                return value(params or {}) if callable(value) else value
        return {}


class StubProvider:
    def __init__(self, payloads):
        self.http = StubHttp(payloads)


class DollarVolumeTest(unittest.TestCase):
    def bars(self, close: float, volume: float):
        return lambda params: {"results": [
            {"T": "AAA", "v": volume, "vw": close, "c": close},
            {"T": "BBB", "v": 10, "vw": 1.0, "c": 1.0},
        ]}

    def test_prices_are_requested_unadjusted(self):
        """THE regression. Adjusted closes are restated onto today's basis;
        multiplied by an as-filed share count they invent a market cap."""
        p = StubProvider({"grouped": self.bars(100.0, 1_000_000)})
        dollar_volume(p, date(2022, 1, 3), sessions=3, min_sessions=1)
        adjusted = {c[1].get("adjusted") for c in p.http.calls}
        self.assertEqual(adjusted, {"false"})

    def test_a_name_that_barely_traded_is_excluded(self):
        """Four busy days out of twenty-five is not a $10M-a-day name."""
        calls = {"n": 0}

        def sometimes(params):
            calls["n"] += 1
            if calls["n"] > 2:
                return {"results": [{"T": "AAA", "v": 1e6, "vw": 100.0, "c": 100.0}]}
            return {"results": [{"T": "AAA", "v": 1e6, "vw": 100.0, "c": 100.0},
                                {"T": "RARE", "v": 1e6, "vw": 100.0, "c": 100.0}]}

        p = StubProvider({"grouped": sometimes})
        out = dollar_volume(p, date(2022, 1, 3), sessions=6, min_sessions=4)
        self.assertIn("AAA", out)
        self.assertNotIn("RARE", out)

    def test_the_close_is_the_one_at_as_of(self):
        seen = {"n": 0}

        def descending(params):
            seen["n"] += 1
            px = 100.0 - seen["n"]
            return {"results": [{"T": "AAA", "v": 1e6, "vw": px, "c": px}]}

        p = StubProvider({"grouped": descending})
        out = dollar_volume(p, date(2022, 1, 3), sessions=3, min_sessions=1)
        self.assertAlmostEqual(out["AAA"][1], 99.0)      # the first session read


class SharesOutstandingTest(unittest.TestCase):
    def frame(self, rows):
        return {"data": rows}

    def test_a_count_filed_after_the_selection_date_is_ignored(self):
        """It was not available when the universe was chosen."""
        p = StubProvider({"frames": self.frame([
            {"cik": 320193, "val": 999.0, "end": "2022-06-30", "entityName": "LATER"},
            {"cik": 320193, "val": 100.0, "end": "2021-09-30", "entityName": "OK"},
        ])})
        out = shares_outstanding(p, date(2022, 1, 3))
        self.assertEqual(out["0000320193"][0], 100.0)

    def test_the_most_recent_available_count_wins(self):
        p = StubProvider({"frames": self.frame([
            {"cik": 1, "val": 10.0, "end": "2020-12-31", "entityName": "OLD"},
            {"cik": 1, "val": 20.0, "end": "2021-12-31", "entityName": "NEW"},
        ])})
        self.assertEqual(shares_outstanding(p, date(2022, 1, 3))["0000000001"][0], 20.0)

    def test_frames_walk_backwards_from_as_of(self):
        self.assertEqual(_frames(date(2022, 1, 3), 3),
                         ["CY2022Q1I", "CY2021Q4I", "CY2021Q3I"])


class StratifiedTest(unittest.TestCase):
    def pool(self):
        """Two names per stratum: a quiet one and a heavily traded one."""
        out = []
        for i in range(10):
            cap = 2e9 + i * 1.8e9
            out.append(cand(f"QUIET{i}", cap, 12e6))
            out.append(cand(f"LOUD{i}", cap * 1.01, 400e6))
        return out

    def test_it_spreads_across_the_capitalisation_band(self):
        """Ranking by liquidity piles the universe into the large end and
        kills the hypothesis's one structural prediction."""
        picked = stratified(self.pool(), 10)
        caps = [c.market_cap for c in picked]
        self.assertEqual(caps, sorted(caps))
        self.assertGreater(max(caps) / min(caps), 5.0)

    def test_it_takes_the_quiet_name_in_each_stratum(self):
        """Selecting on turnover selects for contested prices — the opposite
        of the setup the hypothesis describes."""
        picked = stratified(self.pool(), 10)
        self.assertTrue(all(c.symbol.startswith("QUIET") for c in picked),
                        [c.symbol for c in picked])

    def test_an_ineligible_name_falls_through_to_the_next(self):
        pool = self.pool()
        picked = stratified(pool, 10, eligible=lambda c: not c.symbol.startswith("QUIET"))
        self.assertTrue(picked)
        self.assertTrue(all(c.symbol.startswith("LOUD") for c in picked))

    def test_no_name_is_selected_twice(self):
        picked = stratified(self.pool(), 10, eligible=lambda c: True)
        self.assertEqual(len(picked), len({c.symbol for c in picked}))

    def test_a_stratum_with_nothing_eligible_is_skipped_not_filled(self):
        """Reaching into another stratum to make the count would defeat the
        spread the stratification exists to produce."""
        picked = stratified(self.pool(), 10, eligible=lambda c: c.market_cap < 6e9)
        self.assertTrue(picked)
        self.assertLess(len(picked), 10)
        self.assertTrue(all(c.market_cap < 6e9 for c in picked))

    def test_empty_pool_is_not_an_error(self):
        self.assertEqual(stratified([], 10), [])


if __name__ == "__main__":
    unittest.main()
