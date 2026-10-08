"""Parameter-Stabilitaet: Wie stark aendert sich das Ergebnis, wenn die gewaehlten Parameter leicht verschoben werden?

Um den gewaehlten Punkt wird ein 5x5-Raster aus benachbarten Parameterwerten gerechnet (ohne neue Optimierung,
Kosten und Risiko wie im Lauf). Eine robuste Strategie liegt auf einem Plateau (Nachbarn aehnlich gut); eine einzelne
Spitze, deren Nachbarn deutlich schlechter sind, spricht fuer Ueberanpassung. Beim Train/Test-Split gilt wie
ueberall die Testphase; bei Walk-Forward wechseln die Parameter je Fold, dort ist die Pruefung nicht sinnvoll."""
from statistics import median

from .fmt import de
from .sensitivity import metrics_many

FACTORS = (0.6, 0.8, 1.0, 1.25, 1.5)


def _scaled(v: int, lo: int = 2) -> list:
    return sorted({max(lo, round(v * f)) for f in FACTORS})


def _axes(strategy: str, params: dict):
    """(Name x, Werte x, Name y, Werte y, Funktion (x, y) -> Parameter oder None)."""
    if strategy in ("sma_cross", "combo"):
        def build(f, s):
            return {**params, "fast": f, "slow": s} if f < s else None
        return "fast", _scaled(int(params["fast"])), "slow", _scaled(int(params["slow"])), build
    if strategy == "rsi":
        low = int(params["low"])
        lows = sorted({min(45, max(5, low + d)) for d in (-10, -5, 0, 5, 10)})

        def build(p, lo):
            return {**params, "period": p, "low": lo} if lo < params["high"] else None
        return "period", _scaled(int(params["period"]), 3), "low", lows, build
    if strategy == "bollinger":
        ks = sorted({round(min(4.0, max(0.5, params["k"] + d)), 1) for d in (-1.0, -0.5, 0, 0.5, 1.0)})

        def build(p, k):
            return {**params, "period": p, "k": k}
        return "period", _scaled(int(params["period"]), 5), "k", ks, build
    if strategy == "macd":
        def build(f, sl):
            return {**params, "fast": f, "slow": sl} if f < sl else None
        return "fast", _scaled(int(params["fast"]), 3), "slow", _scaled(int(params["slow"]), 5), build
    if strategy == "donchian":
        def build(en, ex):
            return {**params, "entry": en, "exit": ex} if ex <= en else None
        return "entry", _scaled(int(params["entry"]), 3), "exit", _scaled(int(params["exit"]), 2), build
    if strategy == "momentum":
        th = float(params["threshold"])
        ths = sorted({round(th + d, 1) for d in (-5.0, -2.0, 0, 2.0, 5.0)})

        def build(lb, t):
            return {**params, "lookback": lb, "threshold": t}
        return "lookback", _scaled(int(params["lookback"]), 3), "threshold", ths, build
    return None


def analyze(df, func, params, strategy, kind, validation, fee, ppy, execution, risk) -> dict:
    if kind == "walkforward":
        return {"ok": False, "reason": "bei Walk-Forward wechseln die Parameter je Fold; die Stabilität zeigt dort die Parameter-Tabelle der Folds"}
    ax = _axes(strategy, params)
    if ax is None:
        return {"ok": False, "reason": "für diese Strategie nicht vorgesehen"}
    xn, xs, yn, ys, build = ax
    cx, cy = int(params[xn]), int(params[yn])
    cells = [(j, i, build(x, y)) for j, y in enumerate(ys) for i, x in enumerate(xs)]
    valid = [c for c in cells if c[2] is not None]
    ms = metrics_many(df, func, [c[2] for c in valid], kind, validation, fee, ppy, execution, risk)
    got = {(j, i): m for (j, i, _), m in zip(valid, ms)}
    ret = [[got[(j, i)]["total_return_pct"] if (j, i) in got else None for i in range(len(xs))] for j in range(len(ys))]
    shp = [[got[(j, i)]["sharpe"] if (j, i) in got else None for i in range(len(xs))] for j in range(len(ys))]
    if cx not in xs or cy not in ys or ret[ys.index(cy)][xs.index(cx)] is None:
        return {"ok": False, "reason": "gewählter Parameterpunkt liegt nicht im Raster"}
    ix, iy = xs.index(cx), ys.index(cy)
    center = ret[iy][ix]
    nb = [ret[j][i] for j in range(len(ys)) for i in range(len(xs))
          if (i, j) != (ix, iy) and abs(i - ix) <= 1 and abs(j - iy) <= 1 and ret[j][i] is not None]
    cells = [v for row in ret for v in row if v is not None]
    if not nb:
        return {"ok": False, "reason": "keine gültigen Nachbarwerte im Raster"}
    pos_nb = sum(v > 0 for v in nb) / len(nb)
    nb_med = median(nb)
    level, verdict = _verdict(center, nb, pos_nb, nb_med)
    return {"ok": True, "x_name": xn, "y_name": yn, "x": xs, "y": ys, "ret": ret, "sharpe": shp,
            "center": {"x": cx, "y": cy, "ret": center}, "n_cells": len(cells),
            "share_positive_pct": round(sum(v > 0 for v in cells) / len(cells) * 100, 1),
            "neighbors": len(nb), "neighbors_positive_pct": round(pos_nb * 100, 1),
            "neighbors_median": round(nb_med, 2), "level": level, "verdict": verdict,
            "on_test": kind == "split"}


def _verdict(center, nb, pos_nb, nb_med):
    if center <= 0:
        return "warn", (f"Schon der gewählte Punkt ist nicht profitabel ({de(center)} %); "
                        f"{de(pos_nb * 100, 0)} % der Nachbarn sind im Plus.")
    if pos_nb >= 0.75 and nb_med >= 0.5 * center:
        return "ok", (f"Stabil: {de(pos_nb * 100, 0)} % der Nachbar-Parameter sind im Plus, der Median der Nachbarn "
                      f"liegt bei {de(nb_med)} % (gewählt: {de(center)} %). Das Ergebnis hängt nicht an einem Einzelwert.")
    if pos_nb < 0.5 or nb_med < 0.25 * center:
        return "bad", (f"Spitze statt Plateau: Nur {de(pos_nb * 100, 0)} % der Nachbar-Parameter sind im Plus, der Median der "
                       f"Nachbarn liegt bei {de(nb_med)} % (gewählt: {de(center)} %). Das spricht für Überanpassung an genau diesen Wert.")
    return "warn", (f"Gemischt: {de(pos_nb * 100, 0)} % der Nachbar-Parameter sind im Plus, der Median der Nachbarn liegt bei "
                    f"{de(nb_med)} % (gewählt: {de(center)} %). Das Ergebnis reagiert spürbar auf kleine Änderungen.")
