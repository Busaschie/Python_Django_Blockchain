"""Validierung: Train/Test-Split und Walk-Forward-Analyse.

In beiden Faellen werden Parameter nur auf Trainingsdaten gewaehlt und ausschliesslich auf
davor unbekannten Daten bewertet. Indikatoren nutzen nur vergangene Werte, daher wird das
Signal ueber Train+Test berechnet und danach geschnitten (Warm-up und Carry-in der Position
stammen aus der Trainingsphase, ohne Zukunftsinformation).
Risiko-Einstellungen (Stops, Positionsgroesse) sind fest vorgegeben und nicht Teil der Grid-Search.
"""
import pandas as pd

from . import perf
from .engine import make_curves, run_sim, summarize
from .strategies import GRIDS, STRATEGIES


def _eval_cells(train, strategy, fee, ppy, execution, risk, cells):
    """Sharpe fuer eine Liste von Parameterpunkten (Modulebene, damit der Prozess-Pool sie ausfuehren kann)."""
    func, _ = STRATEGIES[strategy]
    return [run_sim(train, func(train, **params), fee, execution, risk, ppy).metrics(ppy)[0]["sharpe"]
            for _, _, params in cells]


def _grid_search(train: pd.DataFrame, strategy: str, fee: float, ppy: int, execution: str, risk=None,
                 fixed=None):
    key = ("grid", perf.fingerprint(train), strategy, fee, ppy, execution, repr(risk), perf.digest(fixed or {}))
    hit = perf.grid_cache.get(key)
    if hit is not None:
        return hit
    grid = GRIDS[strategy]
    (x_name, xs), (y_name, ys) = grid["x"], grid["y"]
    z = [[None] * len(xs) for _ in ys]  # Sharpe je Kombination (None = ungueltig)
    cells = []
    for j, yv in enumerate(ys):
        for i, xv in enumerate(xs):
            params = grid["build"](xv, yv)
            if params is not None:
                cells.append((i, j, {**(fixed or {}), **params}))  # feste Parameter (z. B. RSI-Teil der Kombi)
    sharpes = None
    if risk is not None and risk.active:    # nur die langsame Kerze-fuer-Kerze-Simulation lohnt den Pool
        sharpes = perf.parallel_map(_eval_cells, (train, strategy, fee, ppy, execution, risk), cells)
    if sharpes is None:
        sharpes = _eval_cells(train, strategy, fee, ppy, execution, risk, cells)
    best = None
    for (i, j, params), sharpe in zip(cells, sharpes):   # feste Reihenfolge: bei Gleichstand gewinnt der erste
        z[j][i] = sharpe
        if best is None or sharpe > best[0]:
            best = (sharpe, params)
    heat = {"x_name": x_name, "x": xs, "y_name": y_name, "y": ys, "z": z}
    result = (best[1], best[0], heat)
    perf.grid_cache.set(key, result)
    return result


def _txt(params: dict) -> str:
    return ", ".join(f"{k}={v}" for k, v in params.items())


def optimize(df: pd.DataFrame, strategy: str, fee: float, periods_per_year: int,
             train_frac: float = 0.7, execution: str = "close", risk=None, fixed=None) -> dict:
    """Einmaliger Split: Parameter auf Train waehlen, einmal auf Test pruefen."""
    cut = int(len(df) * train_frac)
    if cut < 60 or len(df) - cut < 30:
        raise ValueError("Zu wenig Daten fuer Train/Test-Split (Zeitraum oder Zeitfenster vergroessern).")

    func, _ = STRATEGIES[strategy]
    params, _, heat = _grid_search(df.iloc[:cut], strategy, fee, periods_per_year, execution, risk, fixed)

    sim = run_sim(df, func(df, **params), fee, execution, risk, periods_per_year, with_trades=True)
    train_m = sim.metrics(periods_per_year, slice(0, cut))[0]
    test_m = sim.metrics(periods_per_year, slice(cut, None))[0]
    _, equity, buyhold = sim.metrics(periods_per_year)
    overfit = test_m["sharpe"] < 0.5 * train_m["sharpe"] or test_m["total_return_pct"] < 0
    return {
        "params": params,
        "curves": make_curves(df["close"], equity, buyhold, sim.trade_list()),
        "metrics": test_m,
        "validation": {
            "kind": "split", "split_at": df.index[cut].isoformat(), "train_frac": train_frac,
            "lines": [df.index[cut].isoformat()],
            "train": train_m, "test": test_m, "overfit_warning": bool(overfit), "heatmap": heat,
        },
    }


def walk_forward(df: pd.DataFrame, strategy: str, fee: float, periods_per_year: int,
                 n_folds: int = 5, train_mult: int = 3, execution: str = "close", risk=None, fixed=None) -> dict:
    """Rollierend: Fold k trainiert auf `train_mult` Testfenstern und testet auf dem naechsten.
    Die Testfenster aller Folds werden zu einer reinen Out-of-Sample-Kurve zusammengesetzt."""
    T = len(df) // (train_mult + n_folds)  # Laenge eines Testfensters
    train_len = train_mult * T
    if T < 20 or train_len < 60:
        raise ValueError("Zu wenig Daten fuer die Walk-Forward-Analyse (weniger Folds, kleineres "
                         "Train-Fenster oder laengeren Zeitraum waehlen).")
    start0 = len(df) - (train_len + n_folds * T)  # neueste Daten nutzen, Rest am Anfang verwerfen
    func, _ = STRATEGIES[strategy]
    test_sl = slice(train_len, None)

    strats, rets, trade_rets, masks, invs, trades, folds = [], [], [], [], [], [], []
    for k in range(n_folds):
        a = start0 + k * T
        block = df.iloc[a: a + train_len + T]
        params, train_sharpe, _ = _grid_search(block.iloc[:train_len], strategy, fee,
                                               periods_per_year, execution, risk, fixed)
        sim = run_sim(block, func(block, **params), fee, execution, risk, periods_per_year, with_trades=True)
        s, r, tr, mask, inv = sim.parts(test_sl)
        m, _, _ = summarize(s, r, tr, periods_per_year=periods_per_year, exposure=mask, invested=inv)
        strats.append(s); rets.append(r); trade_rets.append(tr); masks.append(mask); invs.append(inv)
        trades += sim.trade_list(start=train_len)
        folds.append({
            "train_start": block.index[0].isoformat()[:10], "test_start": s.index[0].isoformat(),
            "test_end": s.index[-1].isoformat()[:10], "params": params, "params_txt": _txt(params),
            "train_sharpe": round(train_sharpe, 2), "test": m,
        })

    strat_all, ret_all = pd.concat(strats), pd.concat(rets)
    invested = pd.concat(invs) if invs[0] is not None else None
    metrics, equity, buyhold = summarize(strat_all, ret_all, pd.concat(trade_rets),
                                         periods_per_year=periods_per_year, exposure=pd.concat(masks),
                                         invested=invested)
    mean_train = sum(f["train_sharpe"] for f in folds) / len(folds)
    profitable = sum(f["test"]["total_return_pct"] > 0 for f in folds)
    overfit = metrics["sharpe"] < 0.5 * mean_train or metrics["total_return_pct"] < 0
    return {
        "params": folds[-1]["params"],  # zuletzt gewaehlte Parameter (die man einsetzen wuerde)
        "curves": make_curves(df["close"], equity, buyhold, trades),
        "metrics": metrics,
        "validation": {
            "kind": "walkforward", "n_folds": n_folds, "train_mult": train_mult,
            "lines": [f["test_start"] for f in folds[1:]],
            "folds": folds, "train_sharpe_mean": round(mean_train, 2),
            "profitable_folds": f"{profitable}/{n_folds}", "overfit_warning": bool(overfit),
        },
    }
