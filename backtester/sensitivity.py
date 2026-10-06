"""Kosten-Sensitivitaet: dasselbe Ergebnis mit 0-, 1-, 2-, 3- und 5-fachen Kosten (Gebuehr + Slippage).

Die Parameter bleiben fest (keine neue Optimierung), nur die Kosten aendern sich. Beim Train/Test-Split gilt
wieder nur die Testphase, bei Walk-Forward werden die je Fold gewaehlten Parameter erneut gerechnet."""
import pandas as pd

from .fmt import de
from .engine import run_sim, summarize

MULTS = [0, 1, 2, 3, 5]


def _row(m: dict, mult: float, fee: float) -> dict:
    return {"mult": mult, "cost_pct": round(fee * mult * 100, 4), "total_return_pct": m["total_return_pct"],
            "max_drawdown_pct": m["max_drawdown_pct"], "sharpe": m["sharpe"], "trades": m["trades"]}


def _metrics_at(df, func, params, kind, validation, fee, ppy, execution, risk):
    if kind == "walkforward":
        folds, n_folds, mult = validation["folds"], validation["n_folds"], validation["train_mult"]
        T = len(df) // (mult + n_folds)
        train_len = mult * T
        start0 = len(df) - (train_len + n_folds * T)
        parts = []
        for k, f in enumerate(folds):
            block = df.iloc[start0 + k * T: start0 + k * T + train_len + T]
            sim = run_sim(block, func(block, **f["params"]), fee, execution, risk, ppy, with_trades=True)
            parts.append(sim.parts(slice(train_len, None)))
        s = pd.concat([p[0] for p in parts]); r = pd.concat([p[1] for p in parts])
        tr = pd.concat([p[2] for p in parts]); mask = pd.concat([p[3] for p in parts])
        inv = pd.concat([p[4] for p in parts]) if parts[0][4] is not None else None
        return summarize(s, r, tr, periods_per_year=ppy, exposure=mask, invested=inv)[0]
    sim = run_sim(df, func(df, **params), fee, execution, risk, ppy, with_trades=True)
    if kind == "split":
        cut = int(len(df) * validation["train_frac"])
        return sim.metrics(ppy, slice(cut, None))[0]
    return sim.metrics(ppy)[0]


def analyze(df, func, params, kind, validation, fee, ppy, execution, risk, main_metrics) -> dict:
    rows = []
    for mult in MULTS:
        m = _metrics_at(df, func, params, kind, validation, fee * mult, ppy, execution, risk)
        rows.append(_row(m, mult, fee))
    base = next(r for r in rows if r["mult"] == 1)
    replay_diff = abs(base["total_return_pct"] - main_metrics["total_return_pct"])
    # 1x-Zeile aus dem Hauptergebnis uebernehmen (identisch, wenn alles stimmt; Abweichung wird geprueft)
    rows = [_row(main_metrics, 1, fee) if r["mult"] == 1 else r for r in rows]
    be = _break_even(rows)
    return {"ok": True, "fee_pct": round(fee * 100, 4), "rows": rows, "break_even_mult": be,
            "buyhold_return_pct": main_metrics["buyhold_return_pct"], "replay_diff": round(replay_diff, 3),
            "verdict": _verdict(rows, be, fee)}


def _break_even(rows):
    """Kosten-Vielfaches, bei dem die Rendite auf 0 faellt (lineare Interpolation); None = auch bei 5x positiv."""
    if rows[0]["total_return_pct"] <= 0:
        return 0.0
    for a, b in zip(rows, rows[1:]):
        if a["total_return_pct"] > 0 >= b["total_return_pct"]:
            span = a["total_return_pct"] - b["total_return_pct"]
            return round(a["mult"] + (b["mult"] - a["mult"]) * a["total_return_pct"] / span, 2)
    return None


def _verdict(rows, be, fee) -> str:
    last = rows[-1]
    if be is None:
        return (f"Kostenrobust: Auch bei {last['mult']}-fachen Kosten bleibt die Rendite positiv "
                f"({de(last['total_return_pct'])} %).")
    if be == 0:
        return "Schon ohne jegliche Kosten ist die Strategie nicht profitabel – die Kosten sind nicht das Problem."
    pct = de(fee * be * 100, 3)
    if be < 1:
        return (f"Mit den angenommenen Kosten ist das Ergebnis negativ. Der Gewinn verschwindet ab ca. {pct} % "
                f"je Seite ({de(be)}× deiner Annahme).")
    if be < 2:
        return (f"Fragil: Schon bei {de(be)}-fachen Kosten (≈ {pct} % je Seite) ist die Rendite null. Reale Kosten "
                f"und Slippage können das Ergebnis aufzehren.")
    return f"Der Gewinn verschwindet erst bei ca. {de(be)}-fachen Kosten (≈ {pct} % je Seite)."
