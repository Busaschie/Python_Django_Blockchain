"""Tests: Bollinger, MACD, Donchian, Momentum (Handrechnung, kein Blick in die Zukunft, Raster, Formular)."""
from datetime import date

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from . import stability, strategies as st
from .data import synthetic_ohlcv
from .engine import run_backtest
from .forms import BacktestForm
from .validation import optimize, walk_forward

NEW = ("bollinger", "macd", "donchian", "momentum")


def frame(close, spread=0.0):
    idx = pd.date_range("2026-01-01", periods=len(close), freq="D", tz="UTC")
    c = pd.Series(close, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c + spread, "low": c - spread, "close": c, "volume": 1.0}, index=idx)


def big():
    return synthetic_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2025, 6, 30))


class HandComputedTests(SimpleTestCase):
    def test_momentum(self):
        df = frame([10, 11, 12, 11, 10, 12])
        # Änderung über 2 Kerzen: -, -, +20%, 0%, -16,7%, +9,1%  (Schwelle 0 -> nur echte Steigerung)
        self.assertEqual(st.momentum(df, lookback=2, threshold=0).tolist(), [0, 0, 1, 0, 0, 1])
        self.assertEqual(st.momentum(df, lookback=2, threshold=10).tolist(), [0, 0, 1, 0, 0, 0])

    def test_donchian(self):
        df = frame([10, 11, 12, 13, 12, 11, 9, 10, 14])
        # Einstieg: Schluss > Hoch der letzten 3 Kerzen (ohne aktuelle), Ausstieg: Schluss < Tief der letzten 2
        self.assertEqual(st.donchian(df, entry=3, exit=2).tolist(), [0, 0, 0, 1, 1, 0, 0, 0, 1])

    def test_bollinger(self):
        df = frame([10, 10, 10, 10, 8, 9, 10, 11, 10])
        # Fenster 4: Bar 4 (8) liegt unter Mittel-1,0*Std -> Kauf; Verkauf erst, wenn Schluss > Mittelwert
        sig = st.bollinger(df, period=4, k=1.0).tolist()
        self.assertEqual(sig[:4], [0, 0, 0, 0])
        self.assertEqual(sig[4], 1)
        self.assertEqual(sig[-1], 0)
        self.assertTrue(set(sig) <= {0, 1})

    def test_macd_follows_trend(self):
        up = st.macd(frame(np.linspace(100, 200, 120)), 5, 15, 4)
        self.assertEqual(up.iloc[-1], 1)
        down = st.macd(frame(np.linspace(200, 100, 120)), 5, 15, 4)
        self.assertEqual(down.iloc[-1], 0)


class NoLookaheadTests(SimpleTestCase):
    def test_signal_at_t_does_not_depend_on_future(self):
        df = big()
        for name in NEW:
            func, params = st.STRATEGIES[name]
            full = func(df, **params)
            for cut in (150, 260, 380):
                part = func(df.iloc[:cut], **params)
                self.assertEqual(part.tolist(), full.iloc[:cut].tolist(), f"{name} schaut bei {cut} in die Zukunft")

    def test_output_is_zero_one_and_aligned(self):
        df = big()
        for name in NEW:
            func, params = st.STRATEGIES[name]
            sig = func(df, **params)
            self.assertTrue(sig.index.equals(df.index))
            self.assertTrue(set(sig.unique()) <= {0, 1}, name)
            self.assertFalse(sig.isna().any(), name)


class ConfigTests(SimpleTestCase):
    def test_registered_everywhere(self):
        for name in NEW:
            self.assertIn(name, st.STRATEGIES)
            self.assertIn(name, st.LABELS)
            self.assertIn(name, st.GRIDS)
            self.assertEqual(set(st.default_inputs(name)), {"param_a", "param_b", "param_c"})

    def test_default_inputs_reproduce_default_params(self):
        for name in NEW:
            d = st.default_inputs(name)
            self.assertEqual(st.params_from_inputs(name, d["param_a"], d["param_b"], d["param_c"]), st.STRATEGIES[name][1], name)

    def test_grid_builds_valid_parameters(self):
        df = big()
        for name in NEW:
            g = st.GRIDS[name]
            func = st.STRATEGIES[name][0]
            built = [g["build"](x, y) for x in g["x"][1] for y in g["y"][1]]
            built = [b for b in built if b]
            self.assertGreater(len(built), 5, name)
            for b in built:
                func(df, **b)          # darf nicht werfen


class ValidationTests(SimpleTestCase):
    def test_backtest_split_and_walkforward_for_each_strategy(self):
        df = big()
        for name in NEW:
            func, params = st.STRATEGIES[name]
            res = run_backtest(df, func(df, **params), 0.001, periods_per_year=365)
            self.assertIn("total_return_pct", res["metrics"], name)
            opt = optimize(df, name, 0.001, 365, 0.7, "open", None)
            self.assertEqual(set(opt["params"]) <= set(params) | {"signal"}, True, name)
            self.assertIn("heatmap", opt["validation"])
            wf = walk_forward(df, name, 0.001, 365, 3, 2, "open", None)
            self.assertTrue(wf["validation"]["kind"] == "walkforward")

    def test_stability_axes_for_each_strategy(self):
        for name in NEW:
            ax = stability._axes(name, dict(st.STRATEGIES[name][1]))
            self.assertIsNotNone(ax, name)
            self.assertGreaterEqual(len(ax[1]), 3)
            self.assertGreaterEqual(len(ax[3]), 3)
            self.assertTrue(any(ax[4](x, y) for x in ax[1] for y in ax[3]), name)


class FormTests(SimpleTestCase):
    BASE = {"chain": "btc", "mode": "single", "timeframe": "1d", "source": "synthetic", "execution": "open",
            "fee": 0.001, "start_date": "2025-01-01", "end_date": "2025-12-31", "size_mode": "full",
            "exchange": "binance", "param_a": 20, "param_b": 10, "param_c": ""}

    def form(self, **kw):
        return BacktestForm({**self.BASE, **kw})

    def test_valid_defaults(self):
        for name in NEW:
            d = st.default_inputs(name)
            f = self.form(strategy=name, **{k: ("" if v is None else v) for k, v in d.items()})
            self.assertTrue(f.is_valid(), (name, f.errors))
            self.assertEqual(f.strategy_params(), st.STRATEGIES[name][1], name)

    def test_invalid_combinations(self):
        self.assertIn("param_b", self.form(strategy="macd", param_a=26, param_b=12, param_c=9).errors)
        self.assertIn("param_b", self.form(strategy="donchian", param_a=10, param_b=20).errors)
        self.assertIn("param_b", self.form(strategy="bollinger", param_a=20, param_b=0).errors)
        self.assertIn("param_a", self.form(strategy="momentum", param_a=1, param_b=0).errors)
