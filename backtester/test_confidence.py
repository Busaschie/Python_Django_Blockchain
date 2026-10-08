"""Tests: Konfidenzintervalle und Zufallsvergleich mit mehreren Seeds (Monte-Carlo-Modul)."""
import numpy as np
from django.test import SimpleTestCase

from . import montecarlo


def curves(rets, n=400, drift=0.001, seed=1, equity=True):
    rng = np.random.default_rng(seed)
    r = rng.normal(drift, 0.02, n)
    close = (100 * np.cumprod(1 + r)).tolist()
    idx = [f"t{i}" for i in range(n)]
    trades = [{"entry_ts": idx[10 + 12 * i], "exit_ts": idx[16 + 12 * i], "ret_pct": x, "size_pct": 100.0}
              for i, x in enumerate(rets)]
    out = {"index": idx, "close": close, "trades": trades}
    if equity:
        out["strategy"] = (10_000 * np.cumprod(np.concatenate([[1.0], 1 + r[1:]]))).tolist()
    return out


GOOD = [5, -2, 4, 6, -1, 3, 5, -2, 4, 3] * 3
BAD = [-3, 1, -4, 2, -2, -3, 1, -2] * 3


class SeedTests(SimpleTestCase):
    def test_five_seeds_and_median_p(self):
        mc = montecarlo.analyze(curves(GOOD), 0.001, ppy=365)
        self.assertEqual(mc["n_seeds"], 5)
        self.assertEqual(len(mc["p_values"]), 5)
        self.assertEqual((mc["p_min"], mc["p_max"]), (min(mc["p_values"]), max(mc["p_values"])))
        self.assertEqual(mc["p_value"], round(float(np.median(mc["p_values"])), 3))
        self.assertTrue(mc["beats_random"] and mc["beats_random_all_seeds"])

    def test_losing_strategy_never_beats_random(self):
        mc = montecarlo.analyze(curves(BAD), 0.001, ppy=365)
        self.assertFalse(mc["beats_random"])
        self.assertFalse(mc["beats_random_all_seeds"])

    def test_deterministic(self):
        c = curves(GOOD)
        self.assertEqual(montecarlo.analyze(c, 0.001, ppy=365), montecarlo.analyze(c, 0.001, ppy=365))

    def test_ci_does_not_change_bootstrap_values(self):
        c = curves(GOOD)
        a, b = montecarlo.analyze(c, 0.001, ppy=365), montecarlo.analyze(c, 0.001)
        for k in ("return_p5", "return_p50", "return_p95", "dd_median", "dd_p95", "prob_profit", "p_value"):
            self.assertEqual(a[k], b[k])


class IntervalTests(SimpleTestCase):
    def test_intervals_contain_point_and_are_ordered(self):
        ci = montecarlo.analyze(curves(GOOD), 0.001, ppy=365)["ci"]
        self.assertEqual(ci["level"], 95)
        for key in ("mean_trade", "win_rate", "sharpe"):
            x = ci[key]
            self.assertLessEqual(x["lo"], x["point"] + 0.01, key)
            self.assertLessEqual(x["point"] - 0.01, x["hi"], key)
            self.assertLess(x["lo"], x["hi"], key)

    def test_mean_trade_matches_hand_value(self):
        ci = montecarlo.analyze(curves(GOOD), 0.001, ppy=365)["ci"]
        self.assertAlmostEqual(ci["mean_trade"]["point"], float(np.mean(GOOD)), places=2)
        self.assertAlmostEqual(ci["win_rate"]["point"], 70.0, places=2)   # 7 von 10 Trades im Plus
        self.assertTrue(ci["mean_trade"]["sig"])

    def test_noise_is_not_significant(self):
        ci = montecarlo.analyze(curves([2, -2, 3, -3, 1, -1, 2, -2] * 4, drift=0.0), 0.001, ppy=365)["ci"]
        self.assertFalse(ci["mean_trade"]["sig"])

    def test_more_trades_give_narrower_interval(self):
        small = montecarlo.analyze(curves(GOOD[:10] * 1 + GOOD[:10]), 0.001)["ci"]["mean_trade"]
        large = montecarlo.analyze(curves(GOOD * 2, n=900), 0.001)["ci"]["mean_trade"]
        self.assertLess(large["hi"] - large["lo"], small["hi"] - small["lo"])

    def test_sharpe_point_matches_engine_formula(self):
        c = curves(GOOD)
        eq = np.array(c["strategy"])
        r = eq[1:] / eq[:-1] - 1
        expected = r.mean() / r.std(ddof=1) * np.sqrt(365)
        self.assertAlmostEqual(montecarlo.analyze(c, 0.001, ppy=365)["ci"]["sharpe"]["point"], expected, places=2)

    def test_sharpe_skipped_without_equity_or_ppy(self):
        self.assertIsNone(montecarlo.analyze(curves(GOOD), 0.001)["ci"]["sharpe"])
        self.assertIsNone(montecarlo.analyze(curves(GOOD, equity=False), 0.001, ppy=365)["ci"]["sharpe"])
        flat = curves(GOOD); flat["strategy"] = [10_000.0] * 400
        self.assertIsNone(montecarlo.analyze(flat, 0.001, ppy=365)["ci"]["sharpe"])

    def test_too_few_trades_has_no_ci(self):
        mc = montecarlo.analyze(curves([1, 2, 3]), 0.001, ppy=365)
        self.assertFalse(mc["ok"])
        self.assertNotIn("ci", mc)
