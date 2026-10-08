"""Robustheits-Test (Monte-Carlo) auf Basis der Trades eines Laufs.

1. Bootstrap: Die Trades werden N-mal mit Zuruecklegen neu gezogen -> Verteilung von Rendite und
   Max-Drawdown statt einer einzelnen Zahl.
2. Zufallsvergleich: Dieselben Haltedauern an zufaelligen Einstiegszeitpunkten (gleiche Kosten und
   Positionsgroesse), mit mehreren Seeds. Der p-Wert ist der Median ueber die Seeds, die Spanne zeigt,
   ob das Ergebnis vom Zufallsgenerator abhaengt.
3. Konfidenzintervalle (95 %): Sharpe (Block-Bootstrap der Kerzenrenditen, beruecksichtigt Autokorrelation),
   Durchschnitt je Trade und Trefferquote (Bootstrap der Trades). Liegt die Untergrenze nicht ueber 0,
   ist der Vorteil statistisch nicht gesichert.
Rein deterministisch (feste Seeds), keine neuen Daten noetig."""
import numpy as np

N_SIM = 1000
MIN_TRADES = 5       # darunter keine Auswertung
RELIABLE_TRADES = 30  # darunter: statistisch nicht belastbar
EXTRA_SEEDS = (1, 2, 3, 4)   # zusaetzlich zum Haupt-Seed fuer den Zufallsvergleich
CI_LEVEL = 95
MIN_BARS_SHARPE = 30


def _max_dd(equity: np.ndarray) -> np.ndarray:
    """Max-Drawdown je Zeile (equity: Sim x Zeitpunkte, inkl. Startwert 1)."""
    return (equity / np.maximum.accumulate(equity, axis=1) - 1).min(axis=1)


def _random_returns(close, holds, sizes, cost, n_sim, rng) -> np.ndarray:
    """Gesamtrendite (%) von n_sim Zufallsportfolios mit denselben Haltedauern wie die echten Trades."""
    growth = np.ones(n_sim)
    for h, s in zip(holds, sizes):
        start = rng.integers(0, len(close) - h, size=n_sim)
        g = (1 - cost) ** 2 * close[start + h] / close[start] - 1
        growth *= 1 + np.nan_to_num(g) * s
    return (growth - 1) * 100


def _ci(a: np.ndarray, point: float) -> dict:
    lo, hi = np.percentile(a[np.isfinite(a)], [(100 - CI_LEVEL) / 2, 100 - (100 - CI_LEVEL) / 2])
    return {"point": round(float(point), 2), "lo": round(float(lo), 2), "hi": round(float(hi), 2),
            "sig": bool(lo > 0)}   # Untergrenze ueber 0: Vorteil statistisch gesichert


def _sharpe_ci(equity, ppy, n_sim, rng):
    """Block-Bootstrap (Blocklaenge ~ Wurzel n) der Kerzenrenditen -> Konfidenzintervall der Sharpe-Ratio."""
    eq = np.array([e for e in equity if e is not None], dtype=float)
    if len(eq) < MIN_BARS_SHARPE + 1 or not np.all(eq > 0):
        return None
    r = eq[1:] / eq[:-1] - 1
    n = len(r)
    if r.std(ddof=1) == 0:
        return None
    L = max(2, int(round(np.sqrt(n))))
    n_blocks = -(-n // L)
    starts = rng.integers(0, n, size=(n_sim, n_blocks))
    idx = ((starts[:, :, None] + np.arange(L)[None, None, :]) % n).reshape(n_sim, -1)[:, :n]
    draws = r[idx]
    sd = draws.std(axis=1, ddof=1)
    sh = np.where(sd > 0, draws.mean(axis=1) / np.where(sd > 0, sd, 1) * np.sqrt(ppy), np.nan)
    out = _ci(sh, r.mean() / r.std(ddof=1) * np.sqrt(ppy))
    out["block"] = L
    return out


def analyze(curves: dict, cost: float, n_sim: int = N_SIM, seed: int = 42, ppy: int = None) -> dict:
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
        runs = [_random_returns(close, holds, sizes, cost, n_sim, rng)]   # Haupt-Seed: Strom nach dem Bootstrap
        runs += [_random_returns(close, holds, sizes, cost, n_sim, np.random.default_rng(sd)) for sd in EXTRA_SEEDS]
        pooled = np.concatenate(runs)
        # Anteil der Zufallslaeufe, die mindestens so gut sind wie die Strategie (kleiner = besser), je Seed
        ps = [round(float((x >= out["actual_return"]).mean()), 3) for x in runs]
        out.update({
            "random_median": pct(pooled, 50), "random_p95": pct(pooled, 95),
            "p_values": ps, "p_min": min(ps), "p_max": max(ps), "n_seeds": len(ps),
            "p_value": round(float(np.median(ps)), 3),
        })
        out["beats_random"] = out["p_value"] < 0.05
        out["beats_random_all_seeds"] = out["p_max"] < 0.05

    # 3) Konfidenzintervalle (eigener Zufallsstrom: aendert die Werte oben nicht)
    ci_rng = np.random.default_rng(seed + 1000)
    pr = np.array([t["ret_pct"] for t in trades], dtype=float)
    pick = pr[ci_rng.integers(0, n, size=(n_sim, n))]
    ci = {"level": CI_LEVEL, "mean_trade": _ci(pick.mean(axis=1), pr.mean()),
          "win_rate": _ci((pick > 0).mean(axis=1) * 100, (pr > 0).mean() * 100)}
    ci["win_rate"]["sig"] = bool(ci["win_rate"]["lo"] > 50)   # Trefferquote: ueber 50 % gesichert?
    sharpe = _sharpe_ci(curves.get("strategy") or [], ppy, n_sim, ci_rng) if ppy else None
    ci["sharpe"] = sharpe
    out["ci"] = ci
    return out
