"""Tests: Kombinierte Strategie (SMA + RSI), Grid-Search mit festen Parametern, Strategie-Vergleich."""
from datetime import date
from unittest import mock

import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from . import indicators, strategies as st
from .data import PERIODS_PER_YEAR, synthetic_ohlcv
from .forms import BacktestForm
from .models import BacktestRun
from .validation import optimize, walk_forward
from .views import _compare_kind, _compare_payload, _job_for_strategy


def frame():
    idx = pd.date_range("2026-01-01", periods=10, freq="D", tz="UTC")
    return pd.DataFrame({"close": np.arange(10.0) + 100}, index=idx), idx


class ComboSignalTests(SimpleTestCase):
    """Handgerechnet: Trend und RSI werden vorgegeben, die Verknüpfung muss genau diese Folge ergeben."""

    def run_combo(self, logic):
        df, idx = frame()
        fast = pd.Series([1, 3, 3, 3, 3, 3, 1, 1, 3, 3.0], index=idx)   # Trend: F T T T T T F F T T
        slow = pd.Series([2.0] * 10, index=idx)
        rsi = pd.Series([50, 35, 45, 30, 50, 75, 50, 30, 35, 50.0], index=idx)
        with mock.patch.object(indicators, "sma", lambda s, n: {1: fast, 2: slow}[n]), \
                mock.patch.object(indicators, "rsi", lambda s, n: rsi):
            return st.combo(df, fast=1, slow=2, entry=40, exit=70, logic=logic).tolist()

    def test_trend_filter_with_rsi_entry(self):
        # Kauf nur im Aufwärtstrend bei RSI<40; Verkauf bei RSI>70 oder Trendbruch; Bar 7: RSI niedrig, aber kein Trend
        self.assertEqual(self.run_combo("trend"), [0, 1, 1, 1, 1, 0, 0, 0, 1, 1])

    def test_or_logic(self):
        self.assertEqual(self.run_combo("or"), [0, 1, 1, 1, 1, 1, 0, 1, 1, 1])

    def test_never_long_while_trend_is_down_in_trend_mode(self):
        df = synthetic_ohlcv("BTC/USDT", "1d", date(2022, 1, 1), date(2023, 12, 31))
        sig = st.combo(df, fast=20, slow=50)
        trend = indicators.sma(df["close"], 20) > indicators.sma(df["close"], 50)
        self.assertFalse(((sig == 1) & ~trend).any())

    def test_or_is_at_least_as_invested_as_each_part(self):
        df = synthetic_ohlcv("ETH/USDT", "1d", date(2022, 1, 1), date(2023, 12, 31))
        either = st.combo(df, logic="or")
        self.assertTrue((either >= st.sma_cross(df)).all())


class OptimizeWithFixedParamsTests(SimpleTestCase):
    def setUp(self):
        self.df = synthetic_ohlcv("SOL/USDT", "1d", date(2021, 1, 1), date(2023, 12, 31))
        self.ppy = PERIODS_PER_YEAR["1d"]

    def test_grid_is_small_and_fixed_part_is_kept(self):
        valid = sum(1 for x in st.GRIDS["combo"]["x"][1] for y in st.GRIDS["combo"]["y"][1]
                    if st.GRIDS["combo"]["build"](x, y))
        self.assertEqual(valid, 34)  # 6x6 Raster, fast < slow  # nur der SMA-Teil wird durchsucht
        fixed = st.fixed_params("combo", {"rsi_period": 10, "rsi_entry": 35, "rsi_exit": 65, "combo_logic": "or"})
        r = optimize(self.df, "combo", 0.001, self.ppy, 0.7, "open", None, fixed=fixed)
        self.assertEqual({k: r["params"][k] for k in ("period", "entry", "exit", "logic")},
                         {"period": 10, "entry": 35, "exit": 65, "logic": "or"})
        self.assertIn("fast", r["params"])
        self.assertEqual(len(r["validation"]["heatmap"]["z"]), 6)

    def test_walk_forward_keeps_fixed_part_in_every_fold(self):
        fixed = st.fixed_params("combo", {})
        r = walk_forward(self.df, "combo", 0.001, self.ppy, 4, 3, "open", None, fixed=fixed)
        for f in r["validation"]["folds"]:
            self.assertEqual((f["params"]["period"], f["params"]["entry"], f["params"]["exit"], f["params"]["logic"]),
                             (14, 40, 70, "trend"))

    def test_other_strategies_have_no_fixed_part(self):
        self.assertEqual(st.fixed_params("sma_cross", {}), {})
        self.assertEqual(st.fixed_params("rsi", {}), {})


class FormTests(SimpleTestCase):
    base = dict(chain="btc", strategy="combo", mode="single", param_a=10, param_b=40, timeframe="1d", fee=0.001,
                execution="open", source="synthetic", exchange="binance", start_date="2023-01-01", end_date="2023-12-31")

    def test_combo_is_selectable_and_valid(self):
        self.assertIn(("combo", "Kombiniert (SMA + RSI)"), BacktestForm().fields["strategy"].choices)
        f = BacktestForm({**self.base, "combo_logic": "or", "rsi_period": 14, "rsi_entry": 35, "rsi_exit": 65})
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.strategy_params(), {"fast": 10, "slow": 40, "period": 14, "entry": 35, "exit": 65, "logic": "or"})

    def test_rsi_exit_must_be_above_entry(self):
        f = BacktestForm({**self.base, "rsi_entry": 60, "rsi_exit": 50})
        self.assertFalse(f.is_valid())
        self.assertIn("rsi_exit", f.errors)

    def test_defaults_when_rsi_fields_are_empty(self):
        f = BacktestForm(self.base)
        self.assertTrue(f.is_valid(), f.errors)
        self.assertEqual(f.strategy_params()["entry"], 40)


class StrategyCompareTests(SimpleTestCase):
    d = dict(mode="single", strategy="rsi", param_a=99, param_b=98, param_c=97, rsi_entry=33)

    def test_single_mode_uses_defaults_per_strategy(self):
        self.assertEqual({k: _job_for_strategy(self.d, "sma_cross")[k] for k in ("strategy", "param_a", "param_b")},
                         {"strategy": "sma_cross", "param_a": 20, "param_b": 50})
        rsi = _job_for_strategy(self.d, "rsi")
        self.assertEqual((rsi["param_a"], rsi["param_b"], rsi["param_c"]), (14, 30, 70))
        combo = _job_for_strategy(self.d, "combo")
        self.assertEqual((combo["param_a"], combo["param_b"], combo["rsi_entry"]), (20, 50, 33))  # RSI-Feld bleibt

    def test_optimisation_modes_keep_the_form_values(self):
        job = _job_for_strategy({**self.d, "mode": "split"}, "sma_cross")
        self.assertEqual(job["param_a"], 99)  # wird ohnehin von der Grid-Search ersetzt

    def make_runs(self, mode="split"):
        idx = [f"2025-01-{d:02d}T00:00:00+00:00" for d in range(1, 11)]
        runs = []
        for i, s in enumerate(("sma_cross", "rsi", "combo")):
            r = BacktestRun(chain="btc", strategy=s, status="done", job={"mode": mode},
                            validation={"kind": mode, "split_at": idx[5]} if mode == "split" else {},
                            curves={"index": idx, "strategy": [100.0 + i * k for k in range(10)],
                                    "buyhold": [100.0 + 2 * k for k in range(10)]})
            runs.append(r)
        return runs

    def test_kind_detection(self):
        runs = self.make_runs()
        self.assertEqual(_compare_kind(runs), "strategy")
        other = [BacktestRun(chain=c, strategy="rsi") for c in ("btc", "sol", "eth")]
        self.assertEqual(_compare_kind(other), "chain")

    def test_split_mode_shows_only_the_test_phase(self):
        p = _compare_payload(self.make_runs("split"), "strategy")
        self.assertIn("Testphase", p["title"])
        for s in p["series"]:
            self.assertEqual(s["index"][0], "2025-01-06T00:00:00+00:00")  # ab dem Split
            self.assertEqual(len(s["index"]), 5)
            self.assertEqual(len(s["strategy"]), len(s["buyhold"]))
        self.assertEqual([s["name"] for s in p["series"]], ["SMA-Crossover", "RSI", "Kombiniert (SMA + RSI)"])

    def test_single_mode_shows_the_whole_period(self):
        p = _compare_payload(self.make_runs("single"), "strategy")
        self.assertEqual(len(p["series"][0]["index"]), 10)
        self.assertIn("Equity im Vergleich", p["title"])
