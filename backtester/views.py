import uuid
from collections import Counter
from datetime import timedelta

from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from . import jobs
from .chains import CHAINS
from .forms import BacktestForm
from .models import BacktestRun, ExchangeBlock
from .strategies import params_from_inputs

STALE_AFTER = timedelta(minutes=15)


def _history():
    """Gelaufene Auswertungen, gegliedert nach Chain."""
    return [{"key": k, "name": m["name"], "symbol": m["symbol"],
             "total": BacktestRun.objects.filter(chain=k).count(),
             "runs": BacktestRun.objects.filter(chain=k)[:20]} for k, m in CHAINS.items()]


def _expire_stale():
    """Laeufe, die nach einem Server-Neustart haengen geblieben sind, als Fehler markieren."""
    BacktestRun.objects.filter(status__in=["queued", "running"],
                               created_at__lt=timezone.now() - STALE_AFTER
                               ).update(status="error", error="Abgebrochen (Zeitüberschreitung oder Server-Neustart).")


def _initial(run):
    """Formular mit den Einstellungen eines gespeicherten Laufs vorbelegen."""
    end = run.end_date or run.created_at.date()  # ältere Läufe: Zeitraum aus Anzahl Tage und Laufdatum
    period = {"start_date": run.start_date or end - timedelta(days=run.days), "end_date": end}
    if run.job:
        return {**{k: v for k, v in run.job.items() if v is not None}, **period}
    v, p = run.validation or {}, run.params  # Laeufe aus aelteren Versionen
    if run.strategy == "sma_cross":
        a, b, c = p.get("fast"), p.get("slow"), None
    else:
        a, b, c = p.get("period"), p.get("low"), p.get("high")
    initial = {"chain": run.chain, "strategy": run.strategy, "mode": run.mode,
               "timeframe": run.timeframe, **period, "fee": run.fee,
               "execution": run.execution, "source": run.source, "exchange": run.exchange,
               "param_a": a, "param_b": b, "param_c": c}
    if v.get("kind") == "split":
        initial["train_frac"] = round(v["train_frac"] * 100)
    if v.get("kind") == "walkforward":
        initial.update(wf_folds=v["n_folds"], wf_train_mult=v["train_mult"])
    return {k: x for k, x in initial.items() if x is not None}


def _downsample(seq, max_points=600):
    step = max(1, -(-len(seq) // max_points))
    return seq[::step]


def dashboard(request, pk=None, batch=None):
    _expire_stale()
    run = get_object_or_404(BacktestRun, pk=pk) if pk else None
    batch_runs = None
    if batch:
        found = list(BacktestRun.objects.filter(batch=batch))
        if not found:
            raise Http404
        order = list(CHAINS)
        batch_runs = sorted(found, key=lambda r: order.index(r.chain))

    if request.method == "POST":
        form = BacktestForm(request.POST)
        if form.is_valid():
            d = form.cleaned_data
            compare = "compare" in request.POST  # gleiche Einstellungen auf allen drei Chains
            chains = list(CHAINS) if compare else [d["chain"]]
            bid = uuid.uuid4().hex[:10] if compare else ""
            created = []
            for ch in chains:
                new = BacktestRun.objects.create(
                    chain=ch, symbol=CHAINS[ch]["symbol"], timeframe=d["timeframe"],
                    strategy=d["strategy"], days=(d["end_date"] - d["start_date"]).days + 1,
                    start_date=d["start_date"], end_date=d["end_date"], fee=d["fee"], slippage=d["slippage"],
                    source=d["source"],
                    exchange=d["exchange"], execution=d["execution"], status="queued",
                    batch=bid, job={**d, "chain": ch, "start_date": d["start_date"].isoformat(),
                                    "end_date": d["end_date"].isoformat()})
                created.append(new)
                jobs.submit(new.pk)
            return redirect("compare", batch=bid) if compare else redirect("detail", pk=created[0].pk)
    else:
        first = run or (batch_runs[0] if batch_runs else None)
        form = BacktestForm(initial=_initial(first) if first else None)

    exit_reasons = []
    if run and run.status == "done":
        counts = Counter(t.get("reason", "Signal") for t in run.curves.get("trades", []))
        exit_reasons = [(r, counts[r]) for r in ("Signal", "Stop-Loss", "Trailing-Stop", "Take-Profit", "offen")
                        if counts.get(r)]
    shown = [run] if run else (batch_runs or [])
    compare_data = [{"name": r.get_chain_display(), "key": r.chain,
                     "index": _downsample(r.curves["index"]),
                     "strategy": _downsample(r.curves["strategy"]),
                     "buyhold": _downsample(r.curves["buyhold"])}
                    for r in (batch_runs or []) if r.status == "done"]
    return render(request, "backtester/dashboard.html", {
        "form": form, "run": run, "groups": _history(),
        "blocks": ExchangeBlock.objects.filter(until__gt=timezone.now()),
        "run_done": bool(run and run.status == "done"), "exit_reasons": exit_reasons,
        "batch_runs": batch_runs, "compare_data": compare_data,
        "pending_ids": ",".join(str(r.pk) for r in shown if r.is_pending),
        "chains": [{"key": k, **m} for k, m in CHAINS.items()],
    })


def status(request):
    """Status-Abfrage fuer die automatische Aktualisierung waehrend der Berechnung."""
    ids = [int(x) for x in request.GET.get("ids", "").split(",") if x.isdigit()]
    runs = BacktestRun.objects.filter(pk__in=ids)
    return JsonResponse({"runs": [{"id": r.pk, "status": r.status} for r in runs],
                         "pending": sum(r.is_pending for r in runs)})
