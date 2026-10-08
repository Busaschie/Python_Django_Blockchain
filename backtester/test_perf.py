"""Zwischenspeicher und paralleles Rechnen: Ergebnisse dürfen sich dadurch nie ändern."""
import os
from datetime import date
from unittest import mock

from django.test import SimpleTestCase

from . import jobs, perf, sensitivity, stability, validation
from .data import synthetic_ohlcv
from .engine import Risk
from .strategies import STRATEGIES

RISK = Risk.from_inputs(5, 10, None, "full", None)


def frame():
    return synthetic_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2025, 3, 31))


class CacheTests(SimpleTestCase):
    def setUp(self):
        perf.clear_all()

    def test_ttl_cache_hit_miss_eviction_and_expiry(self):
        c = perf.TTLCache(maxsize=2, ttl=10)
        self.assertIsNone(c.get("a"))
        c.set("a", {"x": [1]}); c.set("b", 2); c.set("c", 3)       # "a" ist am längsten unbenutzt -> raus
        self.assertIsNone(c.get("a"))
        self.assertEqual((c.get("b"), c.get("c")), (2, 3))
        with mock.patch("backtester.perf.time.monotonic", return_value=10_000_000):
            self.assertIsNone(c.get("b"))                            # abgelaufen

    def test_cache_returns_copies(self):
        c = perf.TTLCache(4, 60)
        c.set("k", {"v": [1, 2]})
        c.get("k")["v"].append(3)
        self.assertEqual(c.get("k"), {"v": [1, 2]})

    def test_fingerprint_changes_with_data(self):
        df = frame()
        self.assertEqual(perf.fingerprint(df), perf.fingerprint(df.copy()))
        other = df.copy(); other.iloc[5, other.columns.get_loc("close")] *= 1.01
        self.assertNotEqual(perf.fingerprint(df), perf.fingerprint(other))
        self.assertNotEqual(perf.fingerprint(df), perf.fingerprint(df.iloc[:-1]))

    def test_grid_search_is_cached_and_exact(self):
        df = frame()
        first = validation._grid_search(df, "sma_cross", 0.001, 365, "open", RISK)
        with mock.patch("backtester.validation.run_sim", side_effect=AssertionError("neu gerechnet")):
            again = validation._grid_search(df, "sma_cross", 0.001, 365, "open", RISK)
        self.assertEqual(first, again)
        perf.clear_all()
        self.assertEqual(first, validation._grid_search(df, "sma_cross", 0.001, 365, "open", RISK))

    def test_grid_cache_key_covers_all_inputs(self):
        df = frame()
        base = validation._grid_search(df, "sma_cross", 0.001, 365, "open", None)
        calls = []
        real = validation.run_sim
        with mock.patch("backtester.validation.run_sim", side_effect=lambda *a, **k: calls.append(1) or real(*a, **k)):
            validation._grid_search(df, "sma_cross", 0.002, 365, "open", None)     # andere Kosten
            validation._grid_search(df, "sma_cross", 0.001, 365, "close", None)    # andere Ausführung
            validation._grid_search(df, "sma_cross", 0.001, 365, "open", RISK)      # anderes Risiko
            validation._grid_search(df.iloc[:-20], "sma_cross", 0.001, 365, "open", None)   # andere Daten
        self.assertGreater(len(calls), 4 * 20)                                       # jede Variante wurde gerechnet
        self.assertEqual(base, validation._grid_search(df, "sma_cross", 0.001, 365, "open", None))

    def test_stability_second_run_needs_no_simulation(self):
        df = frame()
        res = validation.optimize(df, "sma_cross", 0.001, 365, 0.7, "open", RISK)
        func = STRATEGIES["sma_cross"][0]
        args = (df, func, res["params"], "sma_cross", "split", res["validation"], 0.001, 365, "open", RISK)
        first = stability.analyze(*args)
        with mock.patch("backtester.sensitivity.run_sim", side_effect=AssertionError("neu gerechnet")):
            self.assertEqual(first, stability.analyze(*args))

    def test_cost_sensitivity_center_matches_cache(self):
        df = frame()
        func = STRATEGIES["sma_cross"][0]
        m = sensitivity._metrics_at(df, func, {"fast": 10, "slow": 40}, "single", {}, 0.001, 365, "open", None)
        with mock.patch("backtester.sensitivity.run_sim", side_effect=AssertionError("neu gerechnet")):
            self.assertEqual(m, sensitivity._metrics_at(df, func, {"fast": 10, "slow": 40}, "single", {}, 0.001, 365, "open", None))

    def test_ohlcv_cache_only_for_clean_data(self):
        df = frame()
        with mock.patch("backtester.jobs.fetch_ohlcv", return_value=(df, {"symbol": "BTC/USDT", "note": ""})) as f:
            a = jobs.cached_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2024, 12, 31), "ccxt", "binance")
            b = jobs.cached_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2024, 12, 31), "ccxt", "binance")
            self.assertEqual(f.call_count, 1)
            self.assertTrue(a[0].equals(b[0]))
            b[0].iloc[0, 0] = -1                                    # Änderung am Ergebnis darf den Cache nicht verfälschen
            self.assertNotEqual(jobs.cached_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2024, 12, 31), "ccxt", "binance")[0].iloc[0, 0], -1)
        perf.clear_all()
        with mock.patch("backtester.jobs.fetch_ohlcv", return_value=(df, {"symbol": "BTC/USDT", "note": "Teildaten"})) as f:
            for _ in range(2):
                jobs.cached_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2024, 12, 31), "ccxt", "binance")
            self.assertEqual(f.call_count, 2)                       # mit Hinweis (z. B. Sperre) nicht merken


class ParallelTests(SimpleTestCase):
    def setUp(self):
        perf.clear_all()

    def test_workers_setting(self):
        for value, expected in (("", 1), ("abc", 1), ("0", 1), ("1", 1)):
            with mock.patch.dict(os.environ, {"PARALLEL_WORKERS": value}):
                self.assertEqual(perf.workers(), expected)
        with mock.patch.dict(os.environ, {"PARALLEL_WORKERS": "99"}):
            self.assertLessEqual(perf.workers(), min(8, os.cpu_count() or 1))

    def test_off_by_default_and_small_jobs_stay_serial(self):
        self.assertIsNone(perf.parallel_map(len, (), list(range(50))))                 # PARALLEL_WORKERS nicht gesetzt
        with mock.patch.dict(os.environ, {"PARALLEL_WORKERS": "4"}), mock.patch("backtester.perf.os.cpu_count", return_value=4):
            self.assertIsNone(perf.parallel_map(len, (), list(range(3))))               # zu wenig Arbeit

    def test_pool_failure_falls_back_to_serial(self):
        df = frame()
        serial = validation._grid_search(df, "sma_cross", 0.001, 365, "open", RISK)
        perf.clear_all()
        with mock.patch.dict(os.environ, {"PARALLEL_WORKERS": "2"}), \
                mock.patch("backtester.perf.os.cpu_count", return_value=2), \
                mock.patch("backtester.perf._get_pool", side_effect=RuntimeError("kein Pool")):
            self.assertEqual(serial, validation._grid_search(df, "sma_cross", 0.001, 365, "open", RISK))

    def test_parallel_result_equals_serial_result(self):
        if (os.cpu_count() or 1) < 2:
            self.skipTest("nur ein Prozessorkern")
        df = frame()
        serial = validation._grid_search(df, "sma_cross", 0.001, 365, "open", RISK)
        perf.clear_all()
        seen = []
        real = perf.parallel_map

        def spy(*a, **k):
            seen.append(real(*a, **k))
            return seen[-1]
        try:
            with mock.patch.dict(os.environ, {"PARALLEL_WORKERS": "2"}), \
                    mock.patch("backtester.validation.perf.parallel_map", spy):
                parallel = validation._grid_search(df, "sma_cross", 0.001, 365, "open", RISK)
            self.assertIsNotNone(seen[0], "der Prozess-Pool wurde nicht benutzt")
            self.assertEqual(serial, parallel)
        finally:
            perf.shutdown_pool()
