"""Seiten fuer das Paper-Trading (virtuelles Konto, Journal, Abgleich mit dem Backtest)."""
from django.contrib import messages
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from . import paper, signals
from .forms import UI_TIPS
from .models import BacktestRun, PaperAccount


def _need():
    if not signals.enabled():      # braucht aktuelle Kurse: gleicher Schalter wie das aktuelle Signal
        raise Http404


def _num(text, default):
    try:
        return float(str(text).replace(",", "."))
    except ValueError:
        return default


def paper_list(request):
    _need()
    back = None
    von = request.GET.get("von", "")
    if von.isdigit():                                     # Rücksprung zur Auswertung, von der der Nutzer kam
        back = BacktestRun.objects.filter(pk=int(von), owner=request.user).first()
    return render(request, "backtester/paper_list.html", {
        "back_run": back, "accounts": PaperAccount.objects.filter(owner=request.user), "ui_tips": UI_TIPS,
        "max_accounts": paper.max_accounts()})


def paper_detail(request, pk):
    _need()
    acc = get_object_or_404(PaperAccount, pk=pk, owner=request.user)
    rec = {"ok": False, "reason": "noch keine Prüfung"}
    try:
        if acc.last_price:
            rec = paper.reconcile(acc)
    except Exception as exc:  # noqa: BLE001  (Börse nicht erreichbar)
        rec = {"ok": False, "reason": "Abgleich nicht möglich: " + str(exc)[:150]}
    return render(request, "backtester/paper_detail.html", {
        "acc": acc, "rec": rec, "entries": list(acc.entries.all().order_by("-pk")[:200]),
        "equity": acc.equity_log[-600:], "ui_tips": UI_TIPS})


@require_POST
def paper_start(request, pk):
    _need()
    run = get_object_or_404(BacktestRun, pk=pk, owner=request.user, status="done")
    try:
        acc = paper.start(run, request.user, _num(request.POST.get("capital"), 10_000.0))
    except paper.PaperError as exc:
        messages.error(request, str(exc), extra_tags="signal")
        return redirect("detail", pk=run.pk)
    except Exception as exc:  # noqa: BLE001
        messages.error(request, "Paper-Konto konnte nicht gestartet werden: " + str(exc)[:200], extra_tags="signal")
        return redirect("detail", pk=run.pk)
    messages.success(request, "Paper-Konto gestartet.", extra_tags="signal")
    return redirect("paper_detail", pk=acc.pk)


@require_POST
def paper_check(request, pk):
    _need()
    acc = get_object_or_404(PaperAccount, pk=pk, owner=request.user)
    try:
        res = paper.refresh(acc)
        if res.get("skipped"):
            messages.info(request, "Gerade erst geprüft, bitte kurz warten.")
        else:
            messages.success(request, f"Geprüft: {res['new']} neue Order." if res["new"] else "Geprüft: keine neue Order.")
    except Exception as exc:  # noqa: BLE001
        messages.error(request, "Prüfung fehlgeschlagen: " + str(exc)[:200])
    return redirect("paper_detail", pk=acc.pk)


@require_POST
def paper_toggle(request, pk):
    _need()
    acc = get_object_or_404(PaperAccount, pk=pk, owner=request.user)
    if not acc.active and PaperAccount.objects.filter(owner=request.user, active=True).count() >= paper.max_accounts():
        messages.error(request, f"Höchstens {paper.max_accounts()} aktive Paper-Konten.")
    else:
        acc.active = not acc.active
        acc.save(update_fields=["active"])
        messages.success(request, "Konto läuft weiter." if acc.active else "Konto angehalten (keine Prüfungen mehr).")
    return redirect("paper_detail", pk=acc.pk)


@require_POST
def paper_delete(request, pk):
    _need()
    get_object_or_404(PaperAccount, pk=pk, owner=request.user).delete()
    messages.success(request, "Paper-Konto gelöscht.")
    return redirect("paper_list")
