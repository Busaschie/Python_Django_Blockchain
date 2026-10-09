"""Seiten der KI-Erweiterungen: Laufvergleich, naechste Variante, feste Fragen, Strategie in Klartext."""
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import ai, ai_extra, nl_strategy
from .forms import UI_TIPS
from .models import AiResult, BacktestRun


def ai_context(run, user) -> dict:
    """Zusatzdaten fuer das Dashboard (nur eigener, fertiger Lauf)."""
    if not run or run.status != "done" or run.owner_id != user.pk:
        return {}
    results = list(AiResult.objects.filter(owner=user, run=run, kind__in=("ask", "suggest"), run2__isnull=True))
    suggest = next((r for r in results if r.kind == "suggest"), None)
    return {"ask_questions": ai_extra.QUESTIONS, "ask_periods": [k for k, _, _ in ai_extra.buckets(run)],
            "ask_results": [r for r in results if r.kind == "ask"], "suggest": suggest}


def _back(run):
    return redirect(reverse("detail", args=[run.pk]) + "#aibox")


@require_POST
def ai_suggest(request, pk):
    run = get_object_or_404(BacktestRun, pk=pk, owner=request.user, status="done")
    ai_extra.suggest_next(run, request.user)
    return _back(run)


@require_POST
def ai_ask(request, pk):
    run = get_object_or_404(BacktestRun, pk=pk, owner=request.user, status="done")
    qid, arg = request.POST.get("q", ""), request.POST.get("period", "")
    try:
        ai_extra.ask(run, request.user, qid, arg)
    except ValueError as exc:
        messages.error(request, str(exc), extra_tags="ask")
    return redirect(reverse("detail", args=[run.pk]) + "#askbox")


def ai_compare(request):
    ids = [int(x) for x in request.GET.getlist("run") if x.isdigit()][:2]
    if request.method == "POST":
        ids = [int(x) for x in request.POST.getlist("run") if x.isdigit()][:2]
    runs = list(BacktestRun.objects.filter(owner=request.user, pk__in=ids, status="done"))
    runs.sort(key=lambda r: ids.index(r.pk))
    if len(runs) != 2 or len(set(ids)) != 2:
        messages.error(request, "Bitte genau zwei fertige Läufe in der Liste ankreuzen.")
        return redirect("index")
    a, b = runs
    if request.method == "POST":
        ai_extra.compare_runs(a, b, request.user)
        return redirect(reverse("ai_compare") + f"?run={a.pk}&run={b.pk}")
    return render(request, "backtester/ai_compare.html", {
        "a": a, "b": b, "res": AiResult.objects.filter(owner=request.user, kind="compare", run=a, run2=b).first(),
        "facts": ai_extra.compare_facts(a, b), "ui_tips": UI_TIPS, "ai_left": ai.remaining_today(request.user),
        "ai_limit": ai.user_limit(request.user)})


@require_POST
def nl_strategy_view(request):
    try:
        plan, source = nl_strategy.translate(request.POST.get("text", ""), request.user)
    except nl_strategy.NlError as exc:
        messages.error(request, str(exc), extra_tags="nl")
        return redirect("index")
    messages.success(request, "Verstanden als: " + nl_strategy.describe(plan)
                     + (" (mit KI übersetzt)" if source == "ki" else "") + ". Das Formular ist vorbelegt, prüfe es und starte den Backtest.",
                     extra_tags="nl")
    q = "&".join(f"{k}={v}" for k, v in plan.items() if v is not None)
    return redirect(reverse("index") + f"?plan=1&{q}")
