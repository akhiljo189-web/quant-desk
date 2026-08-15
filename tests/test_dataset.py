"""
The dataset archive: round-trip fidelity and the verification gate.

`ReplayProvider` guarantees that nothing with `known_at` in the simulated
future is served. It cannot guarantee `known_at` is *right*. If the builder or
the loader perturbs a timestamp, the provider serves the corrupted value
faithfully, the engine trades on it, and every other test in the repository
still passes.

So the round trip is asserted field by field, and `verify()` is tested against
archives deliberately corrupted in each way that would matter.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, datetime, timedelta

from qd.config import Mode, Settings
from qd.providers.replay import ReplayDataset
from qd.types import (
    UTC, Bar, EarningsEvent, NewsItem, OptionContract, OptionTrade, Right,
)
from research.dataset import (
    BuildSpec, Manifest, bar_from_dict, bar_to_dict, earnings_from_dict,
    earnings_to_dict, load, news_from_dict, news_to_dict,
    option_trade_from_dict, option_trade_to_dict, save, verify,
)
from research.synthetic import SyntheticSpec, generate

T0 = datetime(2026, 3, 10, 14, 30, tzinfo=UTC)


def a_bar(sym="AAPL", start=T0) -> Bar:
    return Bar(sym, start, start + timedelta(minutes=5),
               100.0, 101.5, 99.25, 100.75, 12345.0, vwap=100.5, trades=87)


def a_news() -> NewsItem:
    return NewsItem(
        id="n-1", symbols=("AAPL", "MSFT"), headline="AAPL raises FY guidance",
        summary="Detail here.", published_at=T0,
        received_at=T0 + timedelta(seconds=37), source="reuters",
        url="https://example.test/a", labels=("guidance_raise:0.75:0.80",),
    )


def an_earnings(with_actuals=True) -> EarningsEvent:
    return EarningsEvent(
        symbol="AAPL",
        report_date=datetime(2026, 1, 28, tzinfo=UTC),
        session="amc",
        scheduled_known_at=datetime(2026, 1, 7, tzinfo=UTC),
        eps_estimate=2.10,
        eps_actual=2.18 if with_actuals else None,
        revenue_estimate=121e9,
        revenue_actual=123.945e9 if with_actuals else None,
        released_at=datetime(2026, 1, 28, 21, 15, tzinfo=UTC) if with_actuals else None,
        fiscal_period="Q1 2026",
    )


def an_option_trade() -> OptionTrade:
    return OptionTrade(
        contract=OptionContract(
            "AAPL", datetime(2026, 4, 17, 21, 0, tzinfo=UTC), 105.0, Right.CALL,
            "O:AAPL260417C00105000",
        ),
        ts=T0, price=2.15, size=250, exchange="CBOE", conditions=("a", "b"),
        nbbo_bid=2.05, nbbo_ask=2.15, underlying_price=101.0,
        open_interest=430, received_at=T0 + timedelta(milliseconds=250),
    )


class RoundTripTest(unittest.TestCase):
    """Every field survives, and every known_at survives EXACTLY."""

    def test_bar_round_trip(self):
        b = a_bar()
        r = bar_from_dict(bar_to_dict(b))
        self.assertEqual(r, b)
        self.assertEqual(r.known_at, b.known_at)

    def test_news_round_trip_preserves_both_timestamps(self):
        n = a_news()
        r = news_from_dict(news_to_dict(n))
        self.assertEqual(r.published_at, n.published_at)
        self.assertEqual(r.received_at, n.received_at)
        self.assertEqual(r.known_at, n.known_at)
        self.assertEqual(r.labels, n.labels)
        self.assertEqual(r.symbols, n.symbols)

    def test_earnings_round_trip_preserves_the_two_gates(self):
        """scheduled_known_at and released_at are separate gates; collapsing
        either one is the earnings leak."""
        e = an_earnings()
        r = earnings_from_dict(earnings_to_dict(e))
        self.assertEqual(r.scheduled_known_at, e.scheduled_known_at)
        self.assertEqual(r.released_at, e.released_at)
        self.assertEqual(r.known_at, e.known_at)
        self.assertEqual(r.actuals_known_at(), e.actuals_known_at())
        self.assertFalse(r.has_actuals_at(r.scheduled_known_at))
        self.assertTrue(r.has_actuals_at(r.released_at))

    def test_unreported_earnings_keeps_released_at_none(self):
        e = an_earnings(with_actuals=False)
        r = earnings_from_dict(earnings_to_dict(e))
        self.assertIsNone(r.released_at)
        self.assertIsNone(r.actuals_known_at())

    def test_option_trade_round_trip(self):
        t = an_option_trade()
        r = option_trade_from_dict(option_trade_to_dict(t))
        self.assertEqual(r.known_at, t.known_at)
        self.assertEqual(r.contract.strike, t.contract.strike)
        self.assertEqual(r.contract.right, t.contract.right)
        self.assertEqual(r.contract.expiry, t.contract.expiry)
        self.assertEqual(r.nbbo_bid, t.nbbo_bid)
        self.assertEqual(r.aggressor(), t.aggressor())
        self.assertEqual(r.open_interest, t.open_interest)


class ArchiveTest(unittest.TestCase):
    def test_full_dataset_survives_save_and_load(self):
        ds = generate(SyntheticSpec(
            symbols=("AAPL", "MSFT"), start=date(2026, 3, 2), end=date(2026, 3, 13),
            seed=3, earnings_every_days=5,
        ))
        with tempfile.TemporaryDirectory() as root:
            save(ds, root)
            back, manifest = load(root)

        self.assertIsNotNone(manifest)
        self.assertEqual(
            sum(len(v) for v in back.daily.values()),
            sum(len(v) for v in ds.daily.values()),
        )
        self.assertEqual(
            sum(len(v) for v in back.bars.values()),
            sum(len(v) for v in ds.bars.values()),
        )
        self.assertEqual(len(back.news), len(ds.news))
        self.assertEqual(len(back.earnings), len(ds.earnings))

    def test_every_known_at_is_bit_identical_after_a_round_trip(self):
        """The assertion the whole module exists for. A shifted timestamp moves
        the simulation's information boundary, invisibly."""
        ds = generate(SyntheticSpec(
            symbols=("AAPL",), start=date(2026, 3, 2), end=date(2026, 3, 13),
            seed=9, earnings_every_days=5,
        ))
        with tempfile.TemporaryDirectory() as root:
            save(ds, root)
            back, _ = load(root)

        before = [b.known_at for b in ds.bars["AAPL"]]
        after = [b.known_at for b in back.bars["AAPL"]]
        self.assertEqual(before, after)

        self.assertEqual(
            sorted(n.known_at for n in ds.news),
            sorted(n.known_at for n in back.news),
        )
        self.assertEqual(
            sorted(e.known_at for e in ds.earnings),
            sorted(e.known_at for e in back.earnings),
        )
        self.assertEqual(
            sorted(str(e.actuals_known_at()) for e in ds.earnings),
            sorted(str(e.actuals_known_at()) for e in back.earnings),
        )

    def test_a_loaded_archive_replays_identically(self):
        """End to end: the archive must produce the same run as the in-memory
        dataset it came from. If it does not, results are not reproducible from
        stored data — which is the only kind anyone can check later."""
        import dataclasses
        from qd.config import Mode, Settings
        from research import replay

        s = Settings.load(Mode.REPLAY)
        s = dataclasses.replace(
            s, universe=dataclasses.replace(s.universe, symbols=("AAPL",))
        )
        ds = generate(SyntheticSpec(
            symbols=("AAPL", "SPY"), start=date(2025, 10, 1), end=date(2026, 1, 15),
            seed=21, earnings_every_days=30,
        ))
        start = datetime(2026, 1, 2, 14, 30, tzinfo=UTC)
        end = datetime(2026, 1, 15, 20, 0, tzinfo=UTC)

        direct = replay.run(s, ds, start, end, journal_path="data/rt_direct.jsonl")
        with tempfile.TemporaryDirectory() as root:
            save(ds, root)
            back, _ = load(root)
        viaarchive = replay.run(s, back, start, end, journal_path="data/rt_archive.jsonl")

        self.assertEqual(direct.count, viaarchive.count)
        self.assertAlmostEqual(direct.expectancy_r(), viaarchive.expectancy_r(), places=9)
        self.assertAlmostEqual(direct.total_pnl(), viaarchive.total_pnl(), places=6)

    def test_partial_write_does_not_leave_a_truncated_file(self):
        """Writes go through a temp file and rename, so an interrupted build
        cannot leave a shorter, entirely plausible history behind."""
        ds = ReplayDataset()
        ds.add_daily("AAPL", [a_bar()])
        with tempfile.TemporaryDirectory() as root:
            save(ds, root)
            leftovers = [
                f for _, _, files in os.walk(root) for f in files if f.endswith(".tmp")
            ]
            self.assertEqual(leftovers, [])


class ManifestTest(unittest.TestCase):
    def test_manifest_round_trip(self):
        m = Manifest(built_at="2026-03-10T00:00:00+00:00",
                     counts={"daily": 10}, warnings=["w"])
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, "manifest.json")
            m.save(p)
            back = Manifest.load(p)
        self.assertEqual(back.counts, {"daily": 10})
        self.assertEqual(back.warnings, ["w"])

    def test_spec_round_trip(self):
        spec = BuildSpec(symbols=("AAPL", "MSFT"),
                         start=date(2026, 1, 1), end=date(2026, 3, 1))
        back = BuildSpec.from_dict(spec.to_dict())
        self.assertEqual(back, spec)

    def test_warmup_precedes_the_decision_window(self):
        spec = BuildSpec(symbols=("AAPL",), start=date(2026, 1, 1),
                         end=date(2026, 3, 1), warmup_days=200)
        self.assertLess(spec.fetch_start, spec.start)

    def test_market_symbol_is_fetched_but_not_traded(self):
        spec = BuildSpec(symbols=("AAPL",), start=date(2026, 1, 1),
                         end=date(2026, 3, 1), market_symbol="SPY")
        self.assertIn("SPY", spec.all_symbols())
        self.assertNotIn("SPY", spec.symbols)


class VerifyTest(unittest.TestCase):
    def _good(self) -> ReplayDataset:
        ds = generate(SyntheticSpec(
            symbols=("AAPL",), start=date(2025, 9, 1), end=date(2026, 3, 1),
            seed=4, earnings_every_days=30,
        ))
        return ds

    def test_a_clean_dataset_passes(self):
        report = verify(self._good())
        self.assertTrue(report.ok, report.explain())

    def test_actuals_readable_at_schedule_time_is_an_error(self):
        """The earnings leak, caught at the archive level rather than after a
        backtest has already produced a beautiful number."""
        ds = ReplayDataset()
        leaked = EarningsEvent(
            symbol="AAPL", report_date=datetime(2026, 1, 28, tzinfo=UTC),
            session="amc",
            scheduled_known_at=datetime(2026, 1, 28, 21, 15, tzinfo=UTC),
            eps_estimate=2.10, eps_actual=2.18,
            released_at=datetime(2026, 1, 28, 21, 15, tzinfo=UTC),
        )
        ds.add_earnings([leaked])
        ds.freeze()
        report = verify(ds)
        self.assertFalse(report.ok)
        self.assertTrue(any("LEAK" in e for e in report.errors), report.explain())

    def test_actuals_without_a_release_time_is_an_error(self):
        ds = ReplayDataset()
        ds.add_earnings([EarningsEvent(
            symbol="AAPL", report_date=datetime(2026, 1, 28, tzinfo=UTC),
            session="amc", scheduled_known_at=datetime(2026, 1, 7, tzinfo=UTC),
            eps_estimate=2.10, eps_actual=2.18, released_at=None,
        )])
        ds.freeze()
        report = verify(ds)
        self.assertFalse(report.ok)
        self.assertTrue(any("released_at" in e for e in report.errors))

    def test_news_received_before_published_is_an_error(self):
        ds = ReplayDataset()
        ds.add_news([NewsItem(
            id="bad", symbols=("AAPL",), headline="x",
            published_at=T0, received_at=T0 - timedelta(minutes=5),
        )])
        ds.freeze()
        report = verify(ds)
        self.assertFalse(report.ok)

    def test_thin_history_is_a_warning_not_an_error(self):
        """Too little history blocks the regime layer but does not corrupt
        anything — the run is uninterpretable, not wrong."""
        ds = ReplayDataset()
        ds.add_daily("AAPL", [
            a_bar("AAPL", T0 + timedelta(days=i)) for i in range(10)
        ])
        ds.freeze()
        report = verify(ds)
        self.assertTrue(report.ok)
        self.assertTrue(any("regime layer" in w for w in report.warnings))

    def test_no_earnings_warns_that_nothing_can_trade(self):
        ds = ReplayDataset()
        ds.add_daily("AAPL", [a_bar("AAPL", T0 + timedelta(days=i)) for i in range(80)])
        ds.freeze()
        report = verify(ds)
        self.assertTrue(any("PEAD trigger cannot fire" in w for w in report.warnings))

    def test_report_reads_cleanly(self):
        text = verify(self._good()).explain()
        self.assertIn("dataset verification", text)
        self.assertIn("daily_bars", text)


if __name__ == "__main__":
    unittest.main()


class AlignToArchiveTest(unittest.TestCase):
    """Config says one bar interval, the archive holds another.

    Not hypothetical: a 5-minute config pointed at a 60-minute archive made
    every bar look 55 minutes overdue, the staleness watchdog halted every
    entry for four years, and the run finished cleanly with zero trades — the
    exact shape of a strategy that never triggers.
    """

    def test_the_archive_wins(self):
        from research.dataset import Manifest, align_to_archive
        s = Settings.load(Mode.REPLAY)
        m = Manifest(spec={"bar_minutes": 60})
        self.assertEqual(align_to_archive(s, m).market.bar_minutes, 60)

    def test_a_matching_config_is_left_alone(self):
        from research.dataset import Manifest, align_to_archive
        s = Settings.load(Mode.REPLAY)
        m = Manifest(spec={"bar_minutes": s.market.bar_minutes})
        self.assertIs(align_to_archive(s, m), s)

    def test_no_manifest_is_not_an_error(self):
        from research.dataset import align_to_archive
        s = Settings.load(Mode.REPLAY)
        self.assertIs(align_to_archive(s, None), s)

    def test_a_manifest_without_a_spec_is_not_an_error(self):
        from research.dataset import Manifest, align_to_archive
        s = Settings.load(Mode.REPLAY)
        self.assertIs(align_to_archive(s, Manifest()), s)
