"""Plausibilitaets-Ampel: automatische Pruefungen je Lauf in drei Gruppen.

Daten      - sind die Kursdaten vollstaendig und plausibel?
Engine     - rechnet die Simulation in sich stimmig (Gegenrechnung aus Kurven/Trades, kein Blick in die Zukunft)?
Aussagekraft - taugt das Ergebnis ueberhaupt fuer eine Schlussfolgerung?
Jede Pruefung liefert "ok", "warn" oder "bad"; die Gesamtampel ist die schlechteste Stufe."""
import numpy as np
import pandas as pd

from .fmt import de

LEVELS = {"ok": 0, "warn": 1, "bad": 2}
TF_MINUTES = {"15m": 15, "1h": 60, "4h": 240, "1d": 1440}


def _c(group, level, title, detail):
    return {"group": group, "level": level, "title": title, "detail": detail}


# ---------------------------------------------------------------- Daten
def check_data(df: pd.DataFrame, timeframe: str) -> list:
    g, out = "Daten", []
    n = len(df)
    out.append(_c(g, "bad" if n < 50 else "warn" if n < 150 else "ok", "Datenmenge",
                  f"{n} Kerzen" + (" (zu wenig für belastbare Aussagen)" if n < 150 else "")))
    idx = df.index
    dup, unsorted = int(idx.duplicated().sum()), not idx.is_monotonic_increasing
    out.append(_c(g, "bad" if dup or unsorted else "ok", "Zeitstempel",
                  "doppelte oder unsortierte Kerzen gefunden" if dup or unsorted else "eindeutig und aufsteigend"))
    step = pd.Timedelta(minutes=TF_MINUTES.get(timeframe, 1440))
    if n > 1 and not unsorted:
        gaps = (pd.Series(idx[1:] - idx[:-1]) > step * 1.5)
        missing = int(((pd.Series(idx[1:] - idx[:-1]) / step) - 1).clip(lower=0)[gaps].round().sum())
        share = missing / (n + missing) * 100
        out.append(_c(g, "bad" if share > 5 else "warn" if share > 1 else "ok", "Lücken in den Kursdaten",
                      f"{missing} fehlende Kerzen ({de(share, 1)} %)" if missing else "keine fehlenden Kerzen"))
    bad_px = int(df[["open", "high", "low", "close"]].isna().any(axis=1).sum()
                 + (df[["open", "high", "low", "close"]] <= 0).any(axis=1).sum())
    incons = int(((df["high"] < df["low"]) | (df["close"] > df["high"] * 1.0001) | (df["close"] < df["low"] * 0.9999)).sum())
    out.append(_c(g, "bad" if bad_px else "warn" if incons else "ok", "Preise gültig",
                  f"{bad_px} Kerzen mit leeren/nicht positiven Preisen" if bad_px else
                  f"{incons} Kerzen mit widersprüchlichen High/Low/Close" if incons else "keine leeren oder widersprüchlichen Werte"))
    jump = df["close"].pct_change().abs()
    big = int((jump > 0.4).sum())
    out.append(_c(g, "warn" if big else "ok", "Extreme Kurssprünge",
                  f"{big} Kerzen mit mehr als 40 % Veränderung (Datenfehler oder Ausnahmeereignis?)" if big
                  else "keine Sprünge über 40 % je Kerze"))
    if "volume" in df and n:
        zero = float((df["volume"] <= 0).mean() * 100)
        out.append(_c(g, "warn" if zero > 5 else "ok", "Handelsvolumen",
                      f"{de(zero, 1)} % der Kerzen ohne Volumen" if zero > 5 else "Volumen vorhanden"))
    return out


# ---------------------------------------------------------------- Engine
def _contrib(t):  # Beitrag eines Trades zum Gesamtkapital
    return t["ret_pct"] / 100 * (t.get("size_pct") or 100.0) / 100


def check_engine(curves: dict, metrics: dict, fee: float, kind: str, causal) -> list:
    g, out = "Engine", []
    strat = np.array(curves.get("strategy") or [], dtype=float)
    bh = np.array(curves.get("buyhold") or [], dtype=float)
    close = np.array([c if c is not None else np.nan for c in curves.get("close") or []], dtype=float)
    trades = curves.get("trades") or []
    if len(strat) > 1:
        dd = float((strat / np.maximum.accumulate(strat) - 1).min() * 100)
        diff = abs(dd - metrics["max_drawdown_pct"])
        out.append(_c(g, "bad" if diff > 0.5 else "ok", "Max Drawdown nachgerechnet",
                      f"Kurve {de(dd)} % vs. Kennzahl {de(metrics['max_drawdown_pct'])} %"))
    if len(bh) > 1 and len(close) == len(bh) and kind == "single":
        calc = (close[-1] / close[0] - 1) * 100
        diff = abs(calc - metrics["buyhold_return_pct"])
        out.append(_c(g, "warn" if diff > 1.0 else "ok", "Buy & Hold nachgerechnet",
                      f"aus Kursen {de(calc)} % vs. Kennzahl {de(metrics['buyhold_return_pct'])} %"))
    if trades:
        # Gesamtrendite aus den Einzeltrades (nur Einzellauf: Kurve = Gesamtzeitraum)
        if kind == "single":
            comp = float(np.prod([1 + _contrib(t) for t in trades]) - 1) * 100
            eq = (metrics["total_return_pct"])
            diff = abs(comp - eq)
            tol = max(1.0, abs(eq) * 0.02)
            out.append(_c(g, "bad" if diff > 3 * tol else "warn" if diff > tol else "ok", "Rendite aus Trades nachgerechnet",
                          f"Trades ergeben {de(comp)} % vs. Kennzahl {de(eq)} %"))
        # Einzeltrade neu berechnet: Kauf/Verkauf-Kurs + Kosten gegen gemeldete Rendite
        errs = []
        for t in trades:
            if t.get("exit_px") and t.get("entry_px") and (t.get("size_pct") or 100) and t.get("reason") in ("Signal", None):
                calc = ((1 - fee) ** 2 * t["exit_px"] / t["entry_px"] - 1) * 100
                errs.append(abs(calc - t["ret_pct"]))
        if errs:
            med = float(np.median(errs))
            out.append(_c(g, "warn" if med > 0.1 else "ok", "Trade-Renditen nachgerechnet",
                          f"Abweichung (Median) {de(med, 3)} Prozentpunkte über {len(errs)} Signal-Trades"))
        ordered, overlap = True, False
        prev_exit = None
        for t in trades:
            if t.get("exit_ts") and t["exit_ts"] < t["entry_ts"]:
                ordered = False
            if prev_exit and t["entry_ts"] < prev_exit:
                overlap = True
            prev_exit = t.get("exit_ts") or prev_exit
        out.append(_c(g, "bad" if not ordered or overlap else "ok", "Trade-Reihenfolge",
                      "Trades überlappen oder enden vor dem Einstieg" if not ordered or overlap
                      else "Trades zeitlich geordnet, keine Überschneidung (Long-only)"))
    out.append(_c(g, "ok" if fee >= 0 else "bad", "Kosten berücksichtigt",
                  f"{de(fee * 100, 3)} % je Seite (Gebühr + Slippage) in jeder Position"))
    cost = curves.get("cost") or {}
    if cost.get("ok"):
        d = cost["replay_diff"]
        out.append(_c(g, "warn" if d > 0.05 else "ok", "Kosten-Neuberechnung stimmt",
                      f"gleiche Parameter neu simuliert: Abweichung {d} Prozentpunkte"))
    if causal is not None:
        out.append(_c(g, "ok" if causal else "bad", "Kein Blick in die Zukunft",
                      "Signale ändern sich nicht, wenn spätere Kerzen fehlen" if causal
                      else "Signale hängen von späteren Kerzen ab: Look-ahead-Fehler"))
    return out


def causality_test(df: pd.DataFrame, func, params: dict) -> bool:
    """Signale auf den ersten 70 % der Daten muessen gleich bleiben, wenn die letzten 30 % fehlen."""
    k = int(len(df) * 0.7)
    full = func(df, **params).iloc[:k].fillna(0).astype(int)
    cut = func(df.iloc[:k], **params).fillna(0).astype(int)
    return bool((full.values == cut.values).all())


# ---------------------------------------------------------------- Aussagekraft
def check_meaning(curves: dict, metrics: dict, validation: dict, params: dict, grid, days: int) -> list:
    g, out = "Aussagekraft", []
    trades = curves.get("trades") or []
    n = metrics.get("trades", len(trades))
    out.append(_c(g, "bad" if n < 5 else "warn" if n < 30 else "ok", "Anzahl Trades",
                  f"{n} Trades" + (" (unter 30: statistisch nicht belastbar)" if 5 <= n < 30 else
                                   " (unter 5: keine Aussage möglich)" if n < 5 else "")))
    out.append(_c(g, "warn" if days < 180 else "ok", "Zeitraum",
                  f"{days} Tage" + (" (unter 180: nur eine Marktphase)" if days < 180 else "")))
    if len(trades) >= 3:
        c = np.array([_contrib(t) for t in trades])
        total = float(np.prod(1 + c) - 1)
        without_best = float(np.prod(1 + np.delete(c, int(np.argmax(c)))) - 1)
        fragile = total > 0 and without_best <= 0
        out.append(_c(g, "warn" if fragile else "ok", "Abhängigkeit vom besten Trade",
                      f"Ohne den besten Trade: {de(without_best * 100, 1)} % statt {de(total * 100, 1)} %" +
                      (" – der Gewinn hängt an einem einzigen Trade" if fragile else "")))
    tim = metrics.get("time_in_market_pct")
    if tim is not None:
        out.append(_c(g, "warn" if tim < 5 else "ok", "Zeit im Markt",
                      f"{de(tim, 1)} %" + (" (sehr selten investiert, wenig Datenbasis)" if tim < 5 else "")))
    sharpe, cagr = metrics.get("sharpe") or 0, metrics.get("cagr_pct") or 0
    odd = sharpe > 3 or cagr > 500
    out.append(_c(g, "warn" if odd else "ok", "Realistische Größenordnung",
                  f"Sharpe {de(sharpe)}, CAGR {de(cagr)} %" + (" – außergewöhnlich gut, zuerst auf Fehler prüfen" if odd else "")))
    cost = curves.get("cost") or {}
    if cost.get("ok") and metrics.get("total_return_pct", 0) > 0:
        be = cost["break_even_mult"]
        out.append(_c(g, "warn" if be is not None and be < 2 else "ok", "Kosten-Robustheit", cost["verdict"]))
    if validation.get("kind") == "split":
        out.append(_c(g, "warn" if validation.get("overfit_warning") else "ok", "Train gegen Test",
                      "Testphase deutlich schlechter als Training: Verdacht auf Überanpassung" if validation.get("overfit_warning")
                      else "Testphase hält gegenüber dem Training"))
    if validation.get("kind") == "walkforward" and validation.get("profitable_folds"):
        out.append(_c(g, "ok", "Walk-Forward", f"profitable Folds: {validation['profitable_folds']}"))
    if grid and isinstance(params, dict):
        edge = [k for k, vals in grid.items() if k in params and params[k] in (min(vals), max(vals))]
        if validation.get("kind") == "split":
            out.append(_c(g, "warn" if edge else "ok", "Parameter am Rand des Suchbereichs",
                          f"{', '.join(edge)} liegt am Rand des Rasters: der beste Wert könnte außerhalb liegen" if edge
                          else "optimale Parameter liegen im Inneren des Suchbereichs"))
    return out


# ---------------------------------------------------------------- Gesamt
def summarize(checks: list) -> dict:
    worst = max((LEVELS[c["level"]] for c in checks), default=0)
    level = ["ok", "warn", "bad"][worst]
    return {"level": level, "checks": checks,
            "n_ok": sum(c["level"] == "ok" for c in checks),
            "n_warn": sum(c["level"] == "warn" for c in checks),
            "n_bad": sum(c["level"] == "bad" for c in checks)}
