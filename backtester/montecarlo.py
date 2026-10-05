"""Robustheits-Test (Monte-Carlo) auf Basis der Trades eines Laufs.

1. Bootstrap: Die Trades werden N-mal mit Zuruecklegen neu gezogen -> Verteilung von Rendite und
   Max-Drawdown statt einer einzelnen Zahl.
2. Zufallsvergleich: Dieselben Haltedauern an zufaelligen Einstiegszeitpunkten (gleiche Kosten und
   Positionsgroesse). Zeigt, ob die Strategie besser ist als Zufall.
Rein deterministisch (fester Seed), keine neuen Daten noetig."""
import numpy as np

N_SIM = 1000
MIN_TRADES = 5       # darunter keine Auswertung
RELIABLE_TRADES = 30  # darunter: statistisch nicht belastbar


def _max_dd(equity: np.ndarray) -> np.ndarray:
    """Max-Drawdown je Zeile (equity: Sim x Zeitpunkte, inkl. Startwert 1)."""
    return (equity / np.maximum.accumulate(equity, axis=1) - 1).min(axis=1)


def analyze(curves: dict, cost: float, n_sim: int = N_SIM, seed: int = 42) -> dict:
    trades = curves.get("trades") or []
    n = len(trades)
    out = {"n_trades": n, "min_trades": MIN_TRADES, "reliable_trades": RELIABLE_TRADES}
    if n < MIN_TRADES:
        return {**out, "ok": False}
    rng = np.random.default_rng(seed)
    size = np.array([(t.get("size_pct") or 100.0) / 100 for t in trades])
    r = np.array([t["ret_pct"] / 100 for t in trades]) * size   # Beitrag zum Gesamtkapital

    # 1) Bootstrap der Trades
    draws = r[rng.integers(0, n, size=(n_sim, n))]
    eq = np.concatenate([np.ones((n_sim, 1)), np.cumprod(1 + draws, axis=1)], axis=1)
    final, dd = (eq[:, -1] - 1) * 100, _max_dd(eq) * 100
    pct = lambda a, q: round(float(np.percentile(a, q)), 2)  # noqa: E731
    actual = np.concatenate([[1.0], np.cumprod(1 + r)])
    fan = {str(q): np.round((np.percentile(eq, q, axis=0) - 1) * 100, 2).tolist() for q in (5, 25, 50, 75, 95)}
    out.update({
        "ok": True, "n_sim": n_sim, "fan": fan, "actual": np.round((actual - 1) * 100, 2).tolist(),
        "return_p5": pct(final, 5), "return_p50": pct(final, 50), "return_p95": pct(final, 95),
        "dd_median": pct(dd, 50), "dd_p95": pct(dd, 5),   # 95-%-Fall: nur 5 % der Laeufe sind schlechter
        "prob_profit": round(float((final > 0).mean() * 100), 1),
        "actual_return": round(float((actual[-1] - 1) * 100), 2),
    })

    # 2) Zufalls-Einstiege mit gleicher Haltedauer
    idx = {ts: i for i, ts in enumerate(curves.get("index") or [])}
    close = np.array([c if c is not None else np.nan for c in (curves.get("close") or [])], dtype=float)
    holds, sizes = [], []
    for t, s in zip(trades, size):
        i0 = idx.get(t["entry_ts"])
        i1 = idx.get(t["exit_ts"]) if t.get("exit_ts") else len(close) - 1
        if i0 is not None and i1 is not None and i1 > i0:
            holds.append(i1 - i0)
            sizes.append(s)
    if len(holds) >= MIN_TRADES and len(close) > max(holds) + 1:
        growth = np.ones(n_sim)
        for h, s in zip(holds, sizes):
            start = rng.integers(0, len(close) - h, size=n_sim)
            g = (1 - cost) ** 2 * close[start + h] / close[start] - 1
            growth *= 1 + np.nan_to_num(g) * s
        rnd = (growth - 1) * 100
        out.update({
            "random_median": pct(rnd, 50), "random_p95": pct(rnd, 95),
            # Anteil der Zufallslaeufe, die mindestens so gut sind wie die Strategie (kleiner = besser)
            "p_value": round(float((rnd >= out["actual_return"]).mean()), 3),
        })
        out["beats_random"] = out["p_value"] < 0.05
    return out
