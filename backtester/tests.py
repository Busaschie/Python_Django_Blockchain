"""Tests der Backtest-Engine: Regression (Risk-Engine ohne Optionen == vektorisierte Engine),
Handrechnungen fuer Stops und Positionsgroesse, kein Look-ahead.   Start: python manage.py test"""
import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from . import strategies as st
from .data import _synthetic
from .engine import Risk, _simulate_risk, run_sim


def bars(rows):
    """rows: [(open, high, low, close), ...] -> DataFrame"""
    idx = pd.date_range("2026-01-01", periods=len(rows), freq="D", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx, dtype=float)


def growth(sim):
    return float((1 + sim.strat).prod())


class RegressionTests(SimpleTestCase):
    """Die Kerze-fuer-Kerze-Engine muss ohne Stops/Sizing dieselben Zahlen liefern wie die schnelle."""

    def test_loop_equals_vectorized(self):
        for symbol in ("BTC/USDT", "SOL/USDT"):
            df = _synthetic(symbol, "1d", 900)
            signals = [st.sma_cross(df, f, s) for f, s in [(5, 30), (10, 40), (20, 50), (15, 100)]]
            signals += [st.rsi_reversion(df, p, lo, 100 - lo) for p, lo in [(14, 30), (7, 20), (21, 35)]]
            for execution in ("open", "close"):
                for cost in (0.0, 0.0015):
                    for sig in signals:
                        a = run_sim(df, sig, cost, execution, None, 365, with_trades=True)
                        b = _simulate_risk(df, sig, cost, execution, Risk(), 365)
                        np.testing.assert_allclose(a.strat.values, b.strat.values, atol=1e-12)
                        self.assertTrue((a.mask.values == b.mask.values).all())
                        np.testing.assert_allclose(a.trade_rets().values, b.trade_rets().values, atol=1e-12)
                        ma, mb = a.metrics(365)[0], b.metrics(365)[0]
                        mb["avg_invested_pct"] = ma["avg_invested_pct"]  # nur im Risk-Pfad befuellt
                        self.assertEqual(ma, mb)
                        self.assertEqual(a.trade_list(), b.trade_list())


class StopTests(SimpleTestCase):
    SIG = pd.Series([1, 1, 1, 1])

    def run_open(self, rows, risk, sig=None, execution="open", cost=0.0):
        df = bars(rows)
        return _simulate_risk(df, pd.Series(sig if sig is not None else [1] * len(df), index=df.index),
                              cost, execution, risk, 365)

    BASE = [(100, 101, 99, 100), (100, 102, 99, 101)]

    def test_stop_loss_intrabar(self):
        sim = self.run_open(self.BASE + [(101, 101, 94, 96), (96, 97, 95, 96)], Risk(sl=0.05))
        t = sim.trades[0]
        self.assertEqual((t["entry_px"], t["exit_px"], t["reason"]), (100.0, 95.0, "Stop-Loss"))
        self.assertAlmostEqual(growth(sim), 0.95, places=9)
        self.assertEqual(len(sim.trades), 1)             # Signal bleibt 1, aber: kein sofortiger Wiedereinstieg
        self.assertEqual(sim.strat.iloc[3], 0.0)

    def test_stop_gap_fills_at_open(self):
        sim = self.run_open(self.BASE + [(92, 93, 90, 91)], Risk(sl=0.05))
        self.assertEqual((sim.trades[0]["exit_px"], sim.trades[0]["reason"]), (92.0, "Stop-Loss"))
        self.assertAlmostEqual(growth(sim), 0.92, places=9)

    def test_take_profit(self):
        sim = self.run_open(self.BASE + [(101, 112, 100, 111)], Risk(tp=0.10))
        self.assertEqual((sim.trades[0]["exit_px"], sim.trades[0]["reason"]), (110.0, "Take-Profit"))
        self.assertAlmostEqual(growth(sim), 1.10, places=9)

    def test_stop_wins_when_both_hit_in_same_bar(self):
        sim = self.run_open(self.BASE + [(101, 112, 94, 100)], Risk(sl=0.05, tp=0.10))
        self.assertEqual(sim.trades[0]["reason"], "Stop-Loss")
        self.assertEqual(sim.trades[0]["exit_px"], 95.0)

    def test_trailing_stop_uses_previous_high(self):
        rows = [(100, 101, 99, 100), (100, 120, 99, 118), (118, 119, 107, 108)]
        sim = self.run_open(rows, Risk(trail=0.10))
        t = sim.trades[0]
        self.assertEqual((t["exit_px"], t["reason"]), (108.0, "Trailing-Stop"))  # 120 * 0.9
        self.assertAlmostEqual(growth(sim), 1.08, places=9)

    def test_close_mode_enters_at_signal_close(self):
        sim = self.run_open(self.BASE + [(101, 101, 94, 96)], Risk(sl=0.05), execution="close")
        t = sim.trades[0]
        self.assertEqual((t["entry_px"], t["exit_px"], t["reason"]), (100.0, 95.0, "Stop-Loss"))
        self.assertAlmostEqual(growth(sim), 0.95, places=9)

    def test_reentry_only_after_new_signal(self):
        rows = self.BASE + [(101, 101, 94, 96)] + [(96, 98, 95, 97)] * 3
        sim = self.run_open(rows, Risk(sl=0.05), sig=[1, 1, 1, 0, 1, 1])
        # Stop in Kerze 2; Signal 1,1,1 -> gesperrt; 0 hebt die Sperre auf; danach Wiedereinstieg
        self.assertEqual([t["reason"] for t in sim.trades], ["Stop-Loss", "offen"])


class SizingTests(SimpleTestCase):
    ROWS = [(100, 101, 99, 100), (100, 102, 99, 101), (101, 102, 100, 102)]

    def test_fixed_size_and_cost_scale_with_position(self):
        df = bars(self.ROWS)
        sig = pd.Series([1, 1, 1], index=df.index)
        sim = _simulate_risk(df, sig, 0.001, "open", Risk(size_mode="fixed", size_value=0.5), 365)
        self.assertAlmostEqual(sim.strat.iloc[1], 0.5 * (101 / 100 - 1) - 0.5 * 0.001, places=12)
        self.assertEqual(sim.trades[0]["size_pct"], 50.0)
        self.assertAlmostEqual(sim.invested.iloc[1], 0.5)

    def test_volatility_target(self):
        rets = np.tile([0.01, -0.01], 30)
        close = 100 * np.cumprod(1 + rets)
        rows = [(close[i - 1] if i else close[0], max(close[i], close[i - 1] if i else close[0]) * 1.001,
                 min(close[i], close[i - 1] if i else close[0]) * 0.999, close[i]) for i in range(len(close))]
        df = bars(rows)
        sig = pd.Series([0] * 29 + [1] * 31, index=df.index)
        vol = df["close"].pct_change().rolling(20).std() * np.sqrt(365)
        for target in (0.10, 5.0):
            sim = _simulate_risk(df, sig, 0.0, "open", Risk(size_mode="vol", size_value=target), 365)
            expected = float(np.clip(target / vol.iloc[29], 0, 1))  # Vola bis zur ersten Signalkerze (Index 29)
            self.assertAlmostEqual(sim.trades[0]["size_pct"], round(expected * 100, 1), places=1)
        self.assertEqual(sim.trades[0]["size_pct"], 100.0)  # Ziel 500 % -> auf 100 % gedeckelt


class NoLookaheadTests(SimpleTestCase):
    def test_future_data_does_not_change_the_past(self):
        df = _synthetic("ETH/USDT", "1d", 600)
        risks = [Risk(sl=0.05, tp=0.12, trail=0.08), Risk(size_mode="vol", size_value=0.4),
                 Risk(trail=0.06, size_mode="fixed", size_value=0.5)]
        for execution in ("open", "close"):
            for risk in risks:
                full = _simulate_risk(df, st.sma_cross(df, 10, 40), 0.001, execution, risk, 365)
                for k in (200, 350, 480):
                    part = df.iloc[:k]
                    sim = _simulate_risk(part, st.sma_cross(part, 10, 40), 0.001, execution, risk, 365)
                    np.testing.assert_allclose(sim.strat.values, full.strat.iloc[:k].values, atol=1e-12)
