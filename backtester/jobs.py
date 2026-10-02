"""Hintergrund-Berechnung: Backtests laufen in einem Thread-Pool, die Seite fragt den Status ab.

Bewusst ohne zusaetzliche Dienste (kein Redis/Broker), damit `runserver` genuegt. Fuer den
Produktivbetrieb laesst sich `submit()` durch Celery oder django-q ersetzen, `compute()` bleibt gleich.
"""
from concurrent.futures import ThreadPoolExecutor

from django.db import connection

from .chains import CHAINS
from .data import PERIODS_PER_YEAR, fetch_ohlcv
from .engine import Risk, run_backtest
from .strategies import STRATEGIES, params_from_inputs
from .validation import optimize, walk_forward

_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="backtest")


def submit(pk: int) -> None:
    _executor.submit(_run, pk)


def compute(run) -> None:
    """Berechnet einen Lauf anhand von run.job und schreibt das Ergebnis ins Modell."""
    j = run.job
    # Kosten je Seite = Gebuehr + Slippage/Spread
    ppy, ex, fee = PERIODS_PER_YEAR[j["timeframe"]], j["execution"], j["fee"] + (j.get("slippage") or 0.0)
    df, info = fetch_ohlcv(CHAINS[run.chain]["symbol"], j["timeframe"], j["days"],
                           j["source"], j.get("exchange", "binance"))
    risk = Risk.from_inputs(j.get("stop_loss"), j.get("take_profit"), j.get("trailing_stop"),
                            j.get("size_mode", "full"), j.get("size_value"))
    if j["mode"] == "split":
        result = optimize(df, j["strategy"], fee, ppy, (j.get("train_frac") or 70) / 100,
                          execution=ex, risk=risk)
        params = result.pop("params")
    elif j["mode"] == "walkforward":
        result = walk_forward(df, j["strategy"], fee, ppy, j.get("wf_folds") or 5,
                              j.get("wf_train_mult") or 3, execution=ex, risk=risk)
        params = result.pop("params")
    else:
        params = params_from_inputs(j["strategy"], j.get("param_a"), j.get("param_b"), j.get("param_c"))
        func, _ = STRATEGIES[j["strategy"]]
        result = run_backtest(df, func(df, **params), fee, periods_per_year=ppy, execution=ex, risk=risk)

    run.symbol, run.data_note, run.params = info["symbol"], info["note"], params
    run.metrics, run.curves = result["metrics"], result["curves"]
    run.validation = result.get("validation", {})
    run.status, run.error = "done", ""


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
