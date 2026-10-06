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
