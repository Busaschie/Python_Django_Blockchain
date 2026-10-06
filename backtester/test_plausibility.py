import numpy as np
import pandas as pd
from django.test import SimpleTestCase

from . import plausibility as pl
from .strategies import STRATEGIES


def frame(n=300, seed=1):
    rng = np.random.default_rng(seed)
    c = 100 * np.cumprod(1 + rng.normal(0.0005, 0.02, n))
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1.0}, index=idx)


def lv(checks, title):
    return next(c["level"] for c in checks if c["title"] == title)


class DataChecks(SimpleTestCase):
    def test_clean_data_ok(self):
        self.assertEqual({c["level"] for c in pl.check_data(frame(), "1d")}, {"ok"})

    def test_gaps_duplicates_and_bad_prices(self):
        df = frame()
        gap = df.drop(df.index[50:80])                      # 30 Kerzen fehlen (~9 %)
        self.assertEqual(lv(pl.check_data(gap, "1d"), "Lücken in den Kursdaten"), "bad")
        dup = pd.concat([df, df.iloc[[10]]]).sort_index()
        self.assertEqual(lv(pl.check_data(dup, "1d"), "Zeitstempel"), "bad")
        bad = df.copy(); bad.iloc[5, bad.columns.get_loc("close")] = 0
        self.assertEqual(lv(pl.check_data(bad, "1d"), "Preise gültig"), "bad")
        jump = df.copy(); jump.iloc[100:, jump.columns.get_loc("close")] *= 3
        self.assertEqual(lv(pl.check_data(jump, "1d"), "Extreme Kurssprünge"), "warn")
        self.assertEqual(lv(pl.check_data(df.iloc[:30], "1d"), "Datenmenge"), "bad")


class EngineChecks(SimpleTestCase):
    def _curves(self):
        strat = [10000 * (1.001 ** i) for i in range(50)]
        return {"strategy": strat, "buyhold": strat, "close": [100 + i for i in range(50)],
                "trades": [{"entry_ts": "2024-01-01", "exit_ts": "2024-01-05", "entry_px": 100.0, "exit_px": 110.0,
                            "ret_pct": round(((1 - 0.001) ** 2 * 1.1 - 1) * 100, 2), "size_pct": 100.0,
                            "reason": "Signal", "open": False}]}

    def test_detects_wrong_drawdown_and_overlap(self):
        c = self._curves()
        m = {"max_drawdown_pct": -25.0, "buyhold_return_pct": 0, "total_return_pct": 9.78}
        self.assertEqual(lv(pl.check_engine(c, m, 0.001, "single", True), "Max Drawdown nachgerechnet"), "bad")
        m["max_drawdown_pct"] = 0.0
        ok = pl.check_engine(c, m, 0.001, "single", True)
        self.assertEqual(lv(ok, "Max Drawdown nachgerechnet"), "ok")
        self.assertEqual(lv(ok, "Trade-Renditen nachgerechnet"), "ok")
        self.assertEqual(lv(ok, "Trade-Reihenfolge"), "ok")
        c["trades"].append({**c["trades"][0], "entry_ts": "2024-01-03", "exit_ts": "2024-01-04"})   # ueberlappt
        self.assertEqual(lv(pl.check_engine(c, m, 0.001, "single", True), "Trade-Reihenfolge"), "bad")

    def test_detects_wrong_trade_return_and_total(self):
        c = self._curves(); c["trades"][0]["ret_pct"] = 25.0
        m = {"max_drawdown_pct": 0.0, "buyhold_return_pct": 0, "total_return_pct": 9.78}
        out = pl.check_engine(c, m, 0.001, "single", True)
        self.assertEqual(lv(out, "Trade-Renditen nachgerechnet"), "warn")
        self.assertEqual(lv(out, "Rendite aus Trades nachgerechnet"), "bad")

    def test_causality_detects_lookahead(self):
        df = frame()
        ok, defaults = STRATEGIES["sma_cross"]
        self.assertTrue(pl.causality_test(df, ok, defaults))
        cheat = lambda d, **kw: (d["close"].shift(-1) > d["close"]).astype(int)   # nutzt die naechste Kerze
        self.assertFalse(pl.causality_test(df, cheat, {}))
        for name, (f, p) in STRATEGIES.items():
            self.assertTrue(pl.causality_test(df, f, p), name)


class MeaningChecks(SimpleTestCase):
    def test_few_trades_and_dependence_on_best_trade(self):
        trades = [{"ret_pct": 50.0, "size_pct": 100.0}, {"ret_pct": -10.0}, {"ret_pct": -10.0}, {"ret_pct": 1.0}]
        m = {"trades": 4, "time_in_market_pct": 30, "sharpe": 1.0, "cagr_pct": 20}
        out = pl.check_meaning({"trades": trades}, m, {}, {}, None, 400)
        self.assertEqual(lv(out, "Anzahl Trades"), "bad")
        self.assertEqual(lv(out, "Abhängigkeit vom besten Trade"), "warn")   # ohne den besten: 0,9*0,9*1,01-1 < 0
        robust = [{"ret_pct": 5.0}] * 40
        out = pl.check_meaning({"trades": robust}, {**m, "trades": 40}, {}, {}, None, 400)
        self.assertEqual(lv(out, "Anzahl Trades"), "ok")
        self.assertEqual(lv(out, "Abhängigkeit vom besten Trade"), "ok")

    def test_edge_of_grid_and_unrealistic_numbers(self):
        m = {"trades": 40, "time_in_market_pct": 50, "sharpe": 4.2, "cagr_pct": 900}
        out = pl.check_meaning({"trades": []}, m, {"kind": "split"}, {"fast": 5, "slow": 100},
                               {"fast": [5, 10, 20], "slow": [50, 100, 200]}, 400)
        self.assertEqual(lv(out, "Realistische Größenordnung"), "warn")
        self.assertEqual(lv(out, "Parameter am Rand des Suchbereichs"), "warn")

    def test_summary_takes_worst_level(self):
        s = pl.summarize([{"level": "ok"}, {"level": "warn"}, {"level": "bad"}])
        self.assertEqual((s["level"], s["n_ok"], s["n_warn"], s["n_bad"]), ("bad", 1, 1, 1))
        self.assertEqual(pl.summarize([{"level": "ok"}])["level"], "ok")


class RegimeAndCostTests(SimpleTestCase):
    def _curves(self, drift_up=0.01, n=300):
        """Erst 150 Kerzen Aufwaertstrend, dann 150 Kerzen Abwaertstrend."""
        rets = np.r_[np.full(n // 2, drift_up), np.full(n // 2, -drift_up)]
        close = 100 * np.cumprod(1 + rets)
        idx = [f"t{i:03d}" for i in range(n)]
        pos = np.r_[np.ones(n // 2 - 1), np.zeros(n - n // 2 + 1)]   # nur im Aufwaertstrend investiert
        strat = 10000 * np.cumprod(1 + np.r_[0.0, rets[1:] * pos[1:]])
        return {"index": idx, "close": close.tolist(), "strategy": strat.tolist(), "buyhold": close.tolist(),
                "trades": [{"entry_ts": idx[0], "exit_ts": idx[n // 2 - 1], "ret_pct": 5.0, "size_pct": 100.0}]}

    def test_regimes_split_up_and_down(self):
        from . import regimes
        out = regimes.analyze(self._curves(), "1d")
        self.assertTrue(out["ok"])
        rows = {r["key"]: r for r in out["rows"]}
        self.assertGreater(rows["up"]["strategy_pct"], 0)
        self.assertLess(rows["down"]["buyhold_pct"], 0)
        self.assertAlmostEqual(rows["down"]["strategy_pct"], 0.0, places=1)   # in der Abwaertsphase nicht investiert
        self.assertAlmostEqual(sum(r["share_pct"] for r in out["rows"]), 100, delta=0.2)
        self.assertEqual(rows["up"]["trades"], 0)    # Einstieg in Kerze 0 liegt vor Ende der Anlaufphase (unklassifiziert)
        self.assertIn("Abwärts", out["hint"])
        self.assertTrue(all(s["key"] in ("up", "down", "side") for s in out["segments"]))

    def test_regimes_too_little_data(self):
        from . import regimes
        self.assertFalse(regimes.analyze({"index": ["a"] * 10, "close": [1.0] * 10, "strategy": [1.0] * 10}, "1d")["ok"])

    def test_break_even_and_verdict(self):
        from . import sensitivity as se
        rows = [{"mult": m, "total_return_pct": r} for m, r in [(0, 20.0), (1, 10.0), (2, 0.0), (3, -10.0), (5, -30.0)]]
        self.assertEqual(se._break_even(rows), 2.0)
        rows[2]["total_return_pct"] = 5.0
        self.assertAlmostEqual(se._break_even(rows), 2.33, places=2)
        self.assertIsNone(se._break_even([{"mult": m, "total_return_pct": 5.0} for m in se.MULTS]))
        self.assertEqual(se._break_even([{"mult": 0, "total_return_pct": -1.0}] + rows[1:]), 0.0)
        self.assertIn("Fragil", se._verdict(rows, 1.5, 0.001))
        self.assertIn("nicht profitabel", se._verdict(rows, 0.0, 0.001))
        self.assertIn("Kostenrobust", se._verdict(rows, None, 0.001))

    def test_cost_sensitivity_replays_exactly_and_costs_hurt(self):
        from . import sensitivity as se
        from .engine import run_backtest
        from .strategies import STRATEGIES
        df = frame(500, seed=7)
        func, params = STRATEGIES["sma_cross"]
        main = run_backtest(df, func(df, **params), 0.002, periods_per_year=365)["metrics"]
        out = se.analyze(df, func, params, "single", {}, 0.002, 365, "close", None, main)
        self.assertEqual(out["replay_diff"], 0.0)
        rets = [r["total_return_pct"] for r in out["rows"]]
        self.assertEqual(rets, sorted(rets, reverse=True))        # mehr Kosten -> nie bessere Rendite
        self.assertEqual(out["rows"][1]["total_return_pct"], main["total_return_pct"])
