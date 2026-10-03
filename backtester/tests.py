"""Tests der Backtest-Engine: Regression (Risk-Engine ohne Optionen == vektorisierte Engine),
Handrechnungen fuer Stops und Positionsgroesse, kein Look-ahead.   Start: python manage.py test"""
from datetime import date, timedelta
from unittest import mock

import ccxt
import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from . import strategies as st
from .data import synthetic_ohlcv
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
            df = synthetic_ohlcv(symbol, "1d", date(2023, 6, 1), date(2025, 11, 16))
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
        df = synthetic_ohlcv("ETH/USDT", "1d", date(2024, 1, 1), date(2025, 8, 22))
        risks = [Risk(sl=0.05, tp=0.12, trail=0.08), Risk(size_mode="vol", size_value=0.4),
                 Risk(trail=0.06, size_mode="fixed", size_value=0.5)]
        for execution in ("open", "close"):
            for risk in risks:
                full = _simulate_risk(df, st.sma_cross(df, 10, 40), 0.001, execution, risk, 365)
                for k in (200, 350, 480):
                    part = df.iloc[:k]
                    sim = _simulate_risk(part, st.sma_cross(part, 10, 40), 0.001, execution, risk, 365)
                    np.testing.assert_allclose(sim.strat.values, full.strat.iloc[:k].values, atol=1e-12)


class DateRangeTests(SimpleTestCase):
    """Zeitraum von-bis: Daten, Formular-Validierung, Börsen-Abruf (mit nachgebauter ccxt-Börse)."""

    def test_same_date_gives_same_price_in_any_range(self):
        a = synthetic_ohlcv("BTC/USDT", "1d", date(2024, 1, 1), date(2024, 12, 31))
        b = synthetic_ohlcv("BTC/USDT", "1d", date(2024, 3, 1), date(2025, 2, 1))
        common = a.index.intersection(b.index)
        self.assertGreater(len(common), 250)
        np.testing.assert_allclose(a.loc[common, "close"], b.loc[common, "close"])
        self.assertEqual(a.index[0].date(), date(2024, 1, 1))
        self.assertEqual(a.index[-1].date(), date(2024, 12, 31))  # Enddatum eingeschlossen

    def test_unfinished_candle_is_not_used(self):
        df = synthetic_ohlcv("ETH/USDT", "1d", date.today() - timedelta(days=100), date.today())
        self.assertLess(df.index[-1].date(), date.today())  # heutige Kerze laeuft noch

    def test_form_validation(self):
        from .forms import BacktestForm
        base = dict(chain="btc", strategy="sma_cross", mode="single", param_a=10, param_b=40, timeframe="1d",
                    fee=0.001, execution="open", source="synthetic", exchange="binance")
        ok = lambda s, e: BacktestForm({**base, "start_date": s, "end_date": e}).is_valid()  # noqa: E731
        today = date.today()
        self.assertTrue(ok("2024-01-01", "2025-01-01"))
        self.assertFalse(ok("2025-01-01", "2024-01-01"))                       # Ende vor Start
        self.assertFalse(ok("2024-01-01", "2024-01-20"))                       # zu kurz
        self.assertFalse(ok("2020-01-01", "2025-01-01"))                       # zu lang (> 1500 Tage)
        self.assertFalse(ok("2025-01-01", (today + timedelta(days=3)).isoformat()))  # Zukunft
        self.assertFalse(ok("2009-01-01", "2010-06-01"))                       # vor 2010
        self.assertTrue(ok((today - timedelta(days=400)).isoformat(), today.isoformat()))

    def _fake_exchange(self, history_days=5000, max_candles=300):
        day = 86_400_000

        class Fake:
            timeframes = {"1d": "1d", "1h": "1h"}
            markets = {"BTC/USDT": {"active": True}}
            calls = 0

            def __init__(self, cfg=None):
                pass

            def load_markets(self):
                return self.markets

            def milliseconds(self):
                import time
                return int(time.time() * 1000)

            def fetch_ohlcv(self, sym, tf, since=None, limit=None):
                Fake.calls += 1
                now = self.milliseconds()
                t = max(since, now - history_days * day) // day * day
                out = []
                while t <= now and len(out) < min(limit, max_candles):
                    out.append([t, 100.0, 101.0, 99.0, 100.5, 1.0])
                    t += day
                return out
        return Fake

    def test_fetch_returns_only_requested_window(self):
        from .data import fetch_ohlcv
        fake = self._fake_exchange()
        with mock.patch.object(ccxt, "binance", fake):
            df, info = fetch_ohlcv("BTC/USDT", "1d", date(2024, 3, 1), date(2025, 2, 28), "ccxt", "binance")
        self.assertEqual(df.index[0].date(), date(2024, 3, 1))
        self.assertEqual(df.index[-1].date(), date(2025, 2, 28))
        self.assertEqual(len(df), 365)
        self.assertGreater(fake.calls, 1)  # in mehreren Etappen geladen
        self.assertEqual(info["note"], "")

    def test_fetch_drops_running_candle_and_reports_short_history(self):
        from .data import fetch_ohlcv
        fake = self._fake_exchange(history_days=200)
        with mock.patch.object(ccxt, "binance", fake):
            df, info = fetch_ohlcv("BTC/USDT", "1d", date.today() - timedelta(days=500), date.today(), "ccxt", "binance")
        self.assertLess(df.index[-1].date(), date.today())
        self.assertIn("lieferte nur", info["note"])
        self.assertLess(len(df), 210)


class IndicatorBackendTests(SimpleTestCase):
    def test_backend_reports_active_library(self):
        from unittest import mock

        from backtester import indicators
        with mock.patch.object(indicators, "talib", None):
            self.assertEqual(indicators.backend(), "pandas (Ersatz)")
        with mock.patch.object(indicators, "talib", object()):
            self.assertEqual(indicators.backend(), "TA-Lib")
