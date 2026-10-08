"""Hintergrund-Berechnung: Backtests laufen in einem Thread-Pool, die Seite fragt den Status ab.

Bewusst ohne zusaetzliche Dienste (kein Redis/Broker), damit `runserver` genuegt. Fuer den
Produktivbetrieb laesst sich `submit()` durch Celery oder django-q ersetzen, `compute()` bleibt gleich.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

from django.db import connection

from . import indicators, montecarlo, perf, plausibility, regimes, sensitivity, stability
from .chains import CHAINS
from .data import PERIODS_PER_YEAR, fetch_ohlcv
from .engine import Risk, run_backtest
from .strategies import GRIDS, STRATEGIES, fixed_params, params_from_inputs
from .validation import optimize, walk_forward

_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="backtest")


def json_safe(obj):
    """NaN/Infinity -> None, numpy-Werte -> Python. PostgreSQL (jsonb) lehnt NaN und Infinity ab."""
    import math
    if isinstance(obj, dict):
        return {k: json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [json_safe(v) for v in obj]
    if hasattr(obj, "item") and not isinstance(obj, (str, bytes)):
        obj = obj.item()
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj


def cached_ohlcv(symbol, timeframe, start, end, source, exchange):
    """Kursdaten mit kurzem Zwischenspeicher (10 Min.): Vergleichslaeufe und Wiederholungen laden nicht neu.
    Nur vollstaendige Daten ohne Hinweis (z. B. Teildaten wegen Boersensperre) werden gemerkt."""
    key = (symbol, timeframe, start, end, source, exchange)
    hit = perf.ohlcv_cache.get(key)
    if hit is not None:
        return hit
    df, info = fetch_ohlcv(symbol, timeframe, start, end, source, exchange)
    if not info.get("note"):
        perf.ohlcv_cache.set(key, (df, info))
    return df, info


def submit(pk: int) -> None:
    _executor.submit(_run, pk)


def compute(run) -> None:
    """Berechnet einen Lauf anhand von run.job und schreibt das Ergebnis ins Modell."""
    j = run.job
    # Kosten je Seite = Gebuehr + Slippage/Spread
    ppy, ex, fee = PERIODS_PER_YEAR[j["timeframe"]], j["execution"], j["fee"] + (j.get("slippage") or 0.0)
    if j.get("start_date"):
        start, end = date.fromisoformat(j["start_date"]), date.fromisoformat(j["end_date"])
    else:  # Job aus einer älteren Version: Zeitraum aus Anzahl Tage
        end = run.created_at.date()
        start = end - timedelta(days=j["days"])
    df, info = cached_ohlcv(CHAINS[run.chain]["symbol"], j["timeframe"], start, end,
                            j["source"], j.get("exchange", "binance"))
    risk = Risk.from_inputs(j.get("stop_loss"), j.get("take_profit"), j.get("trailing_stop"),
                            j.get("size_mode", "full"), j.get("size_value"))
    if j["mode"] == "split":
        result = optimize(df, j["strategy"], fee, ppy, (j.get("train_frac") or 70) / 100,
                          execution=ex, risk=risk,
                          fixed=fixed_params(j["strategy"], j))
        params = result.pop("params")
    elif j["mode"] == "walkforward":
        result = walk_forward(df, j["strategy"], fee, ppy, j.get("wf_folds") or 5,
                              j.get("wf_train_mult") or 3, execution=ex, risk=risk,
                              fixed=fixed_params(j["strategy"], j))
        params = result.pop("params")
    else:
        params = params_from_inputs(j["strategy"], j.get("param_a"), j.get("param_b"), j.get("param_c"), j)
        func, _ = STRATEGIES[j["strategy"]]
        result = run_backtest(df, func(df, **params), fee, periods_per_year=ppy, execution=ex, risk=risk)

    run.symbol, run.data_note, run.params = info["symbol"], info["note"][:200], params
    run.indicator_lib = indicators.backend()
    run.metrics, run.curves = json_safe(result["metrics"]), json_safe(result["curves"])
    run.curves["mc"] = json_safe(montecarlo.analyze(run.curves, fee, ppy=ppy))   # Robustheits-Test (Trades)
    run.validation = json_safe(result.get("validation", {}))
    run.params = json_safe(run.params)
    run.curves["regimes"] = json_safe(_safe(regimes.analyze, run.curves, j["timeframe"],
                                            (result.get("validation") or {}).get("kind", "single"),
                                            (result.get("validation") or {}).get("split_at")))
    run.curves["cost"] = json_safe(_safe(sensitivity.analyze, df, STRATEGIES[j["strategy"]][0], params,
                                         (result.get("validation") or {}).get("kind", "single"),
                                         result.get("validation") or {}, fee, ppy, ex, risk, result["metrics"]))
    run.curves["stab"] = json_safe(_safe(stability.analyze, df, STRATEGIES[j["strategy"]][0], params, j["strategy"],
                                         (result.get("validation") or {}).get("kind", "single"),
                                         result.get("validation") or {}, fee, ppy, ex, risk))
    run.curves["plaus"] = json_safe(_plausibility(run, df, result, params, j, fee))
    run.status, run.error = "done", ""


def _safe(func, *args):
    """Zusatzauswertungen duerfen den Lauf nie scheitern lassen."""
    try:
        return func(*args)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"nicht berechenbar: {str(exc)[:150]}"}


def _plausibility(run, df, result, params, j, fee) -> dict:
    """Plausibilitaets-Ampel (siehe plausibility.py). Ein Fehler in der Pruefung darf den Lauf nie scheitern lassen."""
    try:
        func, defaults = STRATEGIES[j["strategy"]]
        kind = (result.get("validation") or {}).get("kind", "single")
        grid = GRIDS.get(j["strategy"])
        grid = {grid["x"][0]: grid["x"][1], grid["y"][0]: grid["y"][1]} if grid else None
        causal = plausibility.causality_test(df, func, params if isinstance(params, dict) else defaults)
        checks = (plausibility.check_data(df, j["timeframe"])
                  + plausibility.check_engine(result["curves"], result["metrics"], fee, kind, causal,
                                                  (result.get("validation") or {}).get("split_at"))
                  + plausibility.check_meaning(result["curves"], result["metrics"], result.get("validation") or {},
                                               params, grid, run.days or len(df)))
        return plausibility.summarize(checks)
    except Exception as exc:  # noqa: BLE001
        return {"level": "warn", "checks": [{"group": "Engine", "level": "warn", "title": "Prüfung nicht möglich",
                                             "detail": str(exc)[:200]}], "n_ok": 0, "n_warn": 1, "n_bad": 0}


def _run(pk: int) -> None:
    from .models import BacktestRun
    try:
        run = BacktestRun.objects.get(pk=pk)
        run.status = "running"
        run.save(update_fields=["status"])
        try:
            compute(run)
        except Exception as exc:  # Netzwerk, unbekanntes Symbol, zu wenig Daten, ...
            run.status, run.error = "error", str(exc)[:500]
        run.save()
    finally:
        connection.close()  # jeder Thread hat eine eigene DB-Verbindung
