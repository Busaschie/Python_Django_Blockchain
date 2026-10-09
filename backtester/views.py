import secrets
import uuid
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpResponse, JsonResponse
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from accounts import demo

from . import ai, ai_extra, export, jobs, nl_strategy, paper, signals, views_ai
from .chains import CHAINS
from .forms import PARAM_TIPS, SIZE_TIPS, UI_TIPS, BacktestForm
from .models import AiResult, BacktestRun, ExchangeBlock, RunTemplate
from .strategies import STRATEGIES, default_inputs, params_from_inputs

STALE_AFTER = timedelta(minutes=15)


def _history(user, tag="", fav=False):
    """Gelaufene Auswertungen, gegliedert nach Chain; optional nur mit Tag und/oder nur Favoriten."""
    def qs(chain):
        q = BacktestRun.objects.filter(owner=user, chain=chain)
        if tag:
            q = q.filter(tags__icontains=f"|{tag}|")
        return q.filter(favorite=True) if fav else q
    return [{"key": k, "name": m["name"], "symbol": m["symbol"], "total": qs(k).count(), "runs": qs(k)[:50]}
            for k, m in CHAINS.items()]


def _all_tags(user) -> list:
    found = set()
    for t in BacktestRun.objects.filter(owner=user).exclude(tags="").values_list("tags", flat=True):
        found.update(x for x in t.split("|") if x)
    return sorted(found, key=str.lower)


MAX_TAGS, MAX_TAG_LEN, MAX_TEMPLATES, MAX_REPORT_RUNS = 5, 24, 20, export.MAX_REPORT_RUNS


def parse_tags(text: str) -> str:
    """"BTC, test ,Test" -> "|BTC|test|": kurze Stichworte, ohne Dopplungen (Gross-/Kleinschreibung egal), hoechstens 5."""
    seen, out = set(), []
    for raw in (text or "").replace("|", ",").split(","):
        tag = " ".join(raw.split())[:MAX_TAG_LEN]
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return "|" + "|".join(out[:MAX_TAGS]) + "|" if out else ""


def _template_job(d: dict) -> dict:
    """Formularwerte als Vorlage: Zeitraum als Laenge in Tagen, nur JSON-faehige Werte."""
    job = {k: v for k, v in d.items() if k not in ("start_date", "end_date") and v is not None}
    job["period_days"] = (d["end_date"] - d["start_date"]).days + 1
    return job


def _suggestion_initial(user, pk):
    """Formular mit der vorgeschlagenen Variante (naechster Test als Einzellauf mit den neuen Parametern)."""
    res = get_object_or_404(AiResult, pk=pk, owner=user, kind="suggest")
    form = (res.payload or {}).get("form")
    if not form:
        raise Http404
    return {**_initial(res.run), **{k: v for k, v in form.items() if v is not None}}


def _template_initial(t: RunTemplate) -> dict:
    j = dict(t.job)
    days = max(1, int(j.pop("period_days", 365)))
    today = timezone.localdate()
    return {**j, "start_date": today - timedelta(days=days - 1), "end_date": today}


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


def _compare_kind(runs) -> str:
    """"strategy": eine Chain, mehrere Strategien. "chain": dieselbe Strategie auf mehreren Chains."""
    return "strategy" if len({r.chain for r in runs}) == 1 and len(runs) > 1 else "chain"


def _compare_payload(runs, kind) -> dict:
    """Diagrammdaten für den Vergleich. Bei Train/Test-Split nur die Testphase, beim Walk-Forward
    ist die Kurve schon Out-of-Sample; alle Kurven starten bei 100 (Normierung im Browser)."""
    mode = runs[0].mode
    title = {"split": "Testphase im Vergleich (Start = 100)",
             "walkforward": "Out-of-Sample im Vergleich (Start = 100)"}.get(mode, "Equity im Vergleich (Start = 100)")
    series = []
    for r in runs:
        if r.status != "done":
            continue
        idx, strat, bh = r.curves["index"], r.curves["strategy"], r.curves["buyhold"]
        cut = (r.validation or {}).get("split_at") if r.mode == "split" else None
        if cut:
            i = next((k for k, t in enumerate(idx) if t >= cut), 0)
            idx, strat, bh = idx[i:], strat[i:], bh[i:]
        series.append({"name": r.strategy_label if kind == "strategy" else r.get_chain_display(),
                       "key": r.strategy if kind == "strategy" else r.chain,
                       "index": _downsample(idx), "strategy": _downsample(strat), "buyhold": _downsample(bh)})
    return {"kind": kind, "title": title, "series": series}


def _job_for_strategy(d: dict, strategy: str) -> dict:
    """Einstellungen für eine Strategie im Strategie-Vergleich. Im Einzellauf gelten die Standardwerte der
    Strategie (die Parameter-Felder gehören nur zur gewählten Strategie); bei Optimierung wählt die
    Grid-Search die Parameter. Der RSI-Teil von Kombiniert kommt aus den Kombi-Feldern."""
    job = {**d, "strategy": strategy}
    if d["mode"] == "single":
        job.update(default_inputs(strategy))
    return job


def _exit_reasons(run):
    if not (run and run.status == "done"):
        return []
    counts = Counter(t.get("reason", "Signal") for t in run.curves.get("trades", []))
    return [(r, counts[r]) for r in ("Signal", "Stop-Loss", "Trailing-Stop", "Take-Profit", "offen") if counts.get(r)]


def json_clean(job: dict) -> dict:
    return jobs.json_safe(job)


def dashboard(request, pk=None, batch=None):
    _expire_stale()
    run = get_object_or_404(BacktestRun, pk=pk, owner=request.user) if pk else None
    batch_runs = None
    if batch:
        found = list(BacktestRun.objects.filter(owner=request.user, batch=batch))
        if not found:
            raise Http404
        order = list(CHAINS)
        names = list(STRATEGIES)
        batch_runs = sorted(found, key=lambda r: (order.index(r.chain), names.index(r.strategy)))

    if request.method == "POST":
        form = BacktestForm(request.POST)
        if form.is_valid() and "save_template" in request.POST:
            name = " ".join(request.POST.get("template_name", "").split())[:60]
            if not name:
                messages.error(request, "Bitte einen Namen für die Vorlage eingeben.")
            elif (not RunTemplate.objects.filter(owner=request.user, name=name).exists()
                  and RunTemplate.objects.filter(owner=request.user).count() >= MAX_TEMPLATES):
                messages.error(request, f"Höchstens {MAX_TEMPLATES} Vorlagen. Lösche zuerst eine andere.")
            else:
                tpl, created = RunTemplate.objects.update_or_create(
                    owner=request.user, name=name, defaults={"job": json_clean(_template_job(form.cleaned_data))})
                messages.success(request, f"Vorlage „{name}“ {'gespeichert' if created else 'aktualisiert'}.")
                return redirect(f"{reverse('index')}?vorlage={tpl.pk}")
        elif form.is_valid():
            d = form.cleaned_data
            by_chain = "compare" in request.POST               # gleiche Einstellungen auf allen drei Chains
            by_strategy = "compare_strategies" in request.POST  # gleiche Einstellungen mit allen Strategien
            compare = by_chain or by_strategy
            if by_strategy:
                variants = [(d["chain"], s, _job_for_strategy(d, s)) for s in STRATEGIES]
            elif by_chain:
                variants = [(ch, d["strategy"], dict(d)) for ch in CHAINS]
            else:
                variants = [(d["chain"], d["strategy"], dict(d))]
            bid = uuid.uuid4().hex[:10] if compare else ""
            created = []
            if demo.is_demo(request.user) and len(variants) > demo.runs_free(request.user):
                messages.error(request, f"Demo-Konto: höchstens {demo.MAX_RUNS} Läufe. Lösche zuerst ältere Läufe oder lege ein eigenes Konto an.")
                variants = []
            for ch, strategy, job in variants:
                new = BacktestRun.objects.create(
                    owner=request.user, chain=ch, symbol=CHAINS[ch]["symbol"], timeframe=d["timeframe"],
                    strategy=strategy, days=(d["end_date"] - d["start_date"]).days + 1,
                    start_date=d["start_date"], end_date=d["end_date"], fee=d["fee"], slippage=d["slippage"],
                    source=d["source"],
                    exchange=d["exchange"], execution=d["execution"], status="queued",
                    batch=bid, job={**job, "chain": ch, "start_date": d["start_date"].isoformat(),
                                    "end_date": d["end_date"].isoformat()})
                created.append(new)
                jobs.submit(new.pk)
            if created:
                return redirect("compare", batch=bid) if compare else redirect("detail", pk=created[0].pk)
    else:
        first = run or (batch_runs[0] if batch_runs else None)
        if first:
            initial = _initial(first)
        elif request.GET.get("vorschlag", "").isdigit():
            initial = _suggestion_initial(request.user, int(request.GET["vorschlag"]))
        elif request.GET.get("plan") == "1":
            initial = nl_strategy.plan_from_query(request.GET) or None
            if initial:
                initial = {**initial, "mode": "single"}
        elif request.GET.get("vorlage", "").isdigit():
            initial = _template_initial(get_object_or_404(RunTemplate, pk=int(request.GET["vorlage"]), owner=request.user))
        else:
            initial = None
        form = BacktestForm(initial=initial)

    exit_reasons = _exit_reasons(run)
    tag_filter, fav_filter = request.GET.get("tag", "").strip()[:MAX_TAG_LEN], request.GET.get("fav") == "1"
    shown = [run] if run else (batch_runs or [])
    compare_kind = _compare_kind(batch_runs) if batch_runs else ""
    compare_data = _compare_payload(batch_runs, compare_kind) if batch_runs else {}
    return render(request, "backtester/dashboard.html", {
        "form": form, "run": run, "groups": _history(request.user, tag_filter, fav_filter),
        "all_tags": _all_tags(request.user), "tag_filter": tag_filter, "fav_filter": fav_filter,
        "templates": RunTemplate.objects.filter(owner=request.user), "template_max": MAX_TEMPLATES,
        "share_url": request.build_absolute_uri(reverse("shared", args=[run.share_token])) if run and run.share_token else "",
        "blocks": ExchangeBlock.objects.filter(until__gt=timezone.now()),
        "run_done": bool(run and run.status == "done"), "exit_reasons": exit_reasons,
        "batch_runs": batch_runs, "compare_data": compare_data, "compare_kind": compare_kind,
        "tip_data": {"params": PARAM_TIPS, "size": SIZE_TIPS}, "ui_tips": UI_TIPS,
        "pending_ids": ",".join(str(r.pk) for r in shown if r.is_pending),
        "chains": [{"key": k, **m} for k, m in CHAINS.items()],
        "ai_left": ai.remaining_today(request.user), "ai_limit": ai.user_limit(request.user),
        "signals_enabled": signals.enabled(request.user), "signal_max": settings.SIGNAL_MAX_PER_USER,
        "nl_max": nl_strategy.MAX_LEN, "nl_examples": nl_strategy.EXAMPLES,
        **views_ai.ai_context(run, request.user),
    })


def status(request):
    """Status-Abfrage fuer die automatische Aktualisierung waehrend der Berechnung."""
    ids = [int(x) for x in request.GET.get("ids", "").split(",") if x.isdigit()]
    runs = BacktestRun.objects.filter(owner=request.user, pk__in=ids)
    return JsonResponse({"runs": [{"id": r.pk, "status": r.status} for r in runs],
                         "pending": sum(r.is_pending for r in runs)})


@require_POST
def delete_run(request, pk):
    """Eigenen Lauf loeschen (fremde Laeufe: 404). Danach zurueck zur aktuellen Seite bzw. zur Startseite."""
    run = get_object_or_404(BacktestRun, pk=pk, owner=request.user)
    here = request.POST.get("next", "")
    run.delete()
    gone = (f"/run/{pk}/", )
    if here and here not in gone and url_has_allowed_host_and_scheme(here, request.get_host()):
        if not here.startswith("/vergleich/") or BacktestRun.objects.filter(owner=request.user, batch=here.split("/")[2]).exists():
            return redirect(here)
    return redirect("index")


@require_POST
def ai_comment(request, pk):
    """KI-Kommentar auf Knopfdruck (einmal je Lauf; ein regelbasierter Kommentar darf per KI erneuert werden)."""
    run = get_object_or_404(BacktestRun, pk=pk, owner=request.user, status="done")
    if not run.ai_source or run.ai_source == "regeln":
        ai.generate(run, request.user)
    return redirect("detail", pk=run.pk)


def _done_run(request, pk):
    return get_object_or_404(BacktestRun, pk=pk, owner=request.user, status="done")


def _download(data, ctype, name):
    resp = HttpResponse(data, content_type=ctype)
    resp["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


def _fname(run, ext):
    return f"{run.chain}_{run.strategy}_{run.timeframe}_{run.pk}.{ext}"


def export_trades(request, pk):
    run = _done_run(request, pk)
    return _download(export.trades_csv(run), "text/csv; charset=utf-8", "trades_" + _fname(run, "csv"))


def export_pdf(request, pk):
    run = _done_run(request, pk)
    return _download(export.summary_pdf(run), "application/pdf", "auswertung_" + _fname(run, "pdf"))


def _need_signals(request):
    if not signals.enabled(request.user):
        raise Http404


@require_POST
def signal_refresh(request, pk):
    _need_signals(request)
    run = _done_run(request, pk)
    try:
        st = signals.refresh(run)
        if not st.get("ok", True):
            messages.error(request, "Aktuelles Signal nicht berechenbar: " + st["reason"], extra_tags="signal")
    except Exception as exc:  # noqa: BLE001  (Börse nicht erreichbar, Sperre, ...)
        messages.error(request, "Aktuelles Signal nicht berechenbar: " + str(exc)[:200], extra_tags="signal")
    return redirect("detail", pk=run.pk)


@require_POST
def signal_toggle(request, pk):
    _need_signals(request)
    run = _done_run(request, pk)
    if demo.is_demo(request.user):
        messages.error(request, "Im Demo-Konto werden keine Signal-Mails versendet. Mit einem eigenen Konto bekommst du bei jedem Signal-Wechsel eine Mail.", extra_tags="signal")
        return redirect("detail", pk=run.pk)
    if run.signal_alert:
        run.signal_alert = False
        messages.success(request, "Signal-Mail ausgeschaltet.", extra_tags="signal")
    elif not request.user.email:
        messages.error(request, "Für diesen Benutzer ist keine E-Mail-Adresse hinterlegt.", extra_tags="signal")
    elif run.source == "synthetic":
        messages.error(request, "Bei synthetischen Daten gibt es kein aktuelles Signal.", extra_tags="signal")
    elif BacktestRun.objects.filter(owner=request.user, signal_alert=True).count() >= settings.SIGNAL_MAX_PER_USER:
        messages.error(request, f"Höchstens {settings.SIGNAL_MAX_PER_USER} Läufe mit Signal-Mail gleichzeitig.", extra_tags="signal")
    else:
        try:
            run.signal_state = signals.current(run)   # Ausgangszustand: erst ein WECHSEL löst eine Mail aus
            if not run.signal_state.get("ok"):
                raise ValueError(run.signal_state.get("reason", "nicht berechenbar"))
            run.signal_alert = True
            messages.success(request, "Signal-Mail eingeschaltet: Du bekommst eine Mail, sobald sich das Signal ändert.", extra_tags="signal")
        except Exception as exc:  # noqa: BLE001
            run.signal_state = {}
            messages.error(request, "Konnte nicht eingeschaltet werden: " + str(exc)[:200], extra_tags="signal")
    run.save(update_fields=["signal_alert", "signal_state"])
    return redirect("detail", pk=run.pk)


@login_not_required
def signal_check(request):
    """Wird von einem externen Zeitplan aufgerufen (z. B. cron-job.org), geschützt durch SIGNAL_CRON_TOKEN."""
    import hmac
    token = settings.SIGNAL_CRON_TOKEN
    if not signals.enabled() or not token:
        raise Http404
    given = request.headers.get("Authorization", "").removeprefix("Bearer ") or request.GET.get("token", "")
    if not hmac.compare_digest(given.encode(), token.encode()):
        return JsonResponse({"error": "unauthorized"}, status=403)
    result = signals.check_all(f"{request.scheme}://{request.get_host()}")
    return JsonResponse({**result, "paper": paper.check_all()})


# --------------------------------------------------------------------------------------
# Teilen, Bericht, Favoriten, Tags, Vorlagen
# --------------------------------------------------------------------------------------
def _back(request, pk=None):
    """Zurueck zur Seite, von der die Aktion kam (nur eigene Adressen), sonst zum Lauf bzw. zur Startseite."""
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, request.get_host()):
        return redirect(nxt)
    return redirect("detail", pk=pk) if pk else redirect("index")


@require_POST
def share_toggle(request, pk):
    run = _done_run(request, pk)
    if demo.is_demo(request.user):
        messages.error(request, "Im Demo-Konto lassen sich Auswertungen nicht teilen. Mit einem eigenen Konto erzeugst du einen schreibgeschützten Link.")
        return redirect("detail", pk=run.pk)
    action = request.POST.get("action")
    if action == "off":
        run.share_token = ""
        messages.success(request, "Freigabe beendet: Der Link funktioniert nicht mehr.")
    elif action == "renew" or (action == "on" and not run.share_token):
        run.share_token = secrets.token_urlsafe(18)
        messages.success(request, "Öffentlicher Link erzeugt." if action == "on" else "Neuer Link erzeugt, der alte funktioniert nicht mehr.")
    run.save(update_fields=["share_token"])
    return redirect("detail", pk=run.pk)


def _shared_run(token):
    if len(token) < 20:
        raise Http404
    return get_object_or_404(BacktestRun, share_token=token, status="done")


def _public(resp):
    resp["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    resp["Referrer-Policy"] = "no-referrer"
    resp["Cache-Control"] = "private, max-age=0, no-cache"
    return resp


@login_not_required
def shared(request, token):
    """Nur-Lesen-Ansicht einer freigegebenen Auswertung (ohne Anmeldung, ohne Namen und E-Mail des Besitzers)."""
    run = _shared_run(token)
    return _public(render(request, "backtester/dashboard.html", {
        "shared": True, "run": run, "run_done": True, "exit_reasons": _exit_reasons(run), "pending_ids": "",
        "tip_data": {"params": PARAM_TIPS, "size": SIZE_TIPS}, "ui_tips": UI_TIPS, "signals_enabled": False,
        "groups": [], "blocks": [], "batch_runs": None, "form": None, "chains": []}))


@login_not_required
def shared_trades(request, token):
    run = _shared_run(token)
    return _public(_download(export.trades_csv(run), "text/csv; charset=utf-8", "trades_" + _fname(run, "csv")))


@login_not_required
def shared_pdf(request, token):
    run = _shared_run(token)
    return _public(_download(export.summary_pdf(run), "application/pdf", "auswertung_" + _fname(run, "pdf")))


def export_report(request):
    """PDF-Bericht aus mehreren eigenen, fertigen Laeufen (?run=1&run=2 oder ?batch=<Vergleich>)."""
    done = BacktestRun.objects.filter(owner=request.user, status="done")
    batch = request.GET.get("batch", "")
    if batch:
        found = list(done.filter(batch=batch))
        order, names = list(CHAINS), list(STRATEGIES)
        runs = sorted(found, key=lambda r: (order.index(r.chain), names.index(r.strategy)))
    else:
        ids = list(dict.fromkeys(int(x) for x in request.GET.getlist("run") if x.isdigit()))[:MAX_REPORT_RUNS]
        by_pk = {r.pk: r for r in done.filter(pk__in=ids)}
        runs = [by_pk[i] for i in ids if i in by_pk]
    if not runs:
        messages.error(request, "Bitte links in der Liste mindestens einen fertigen Lauf ankreuzen.")
        return redirect("index")
    return _download(export.report_pdf(runs), "application/pdf", f"bericht_{len(runs)}_laeufe.pdf")


@require_POST
def favorite_toggle(request, pk):
    run = get_object_or_404(BacktestRun, pk=pk, owner=request.user)
    run.favorite = not run.favorite
    run.save(update_fields=["favorite"])
    return _back(request, pk)


@require_POST
def tags_set(request, pk):
    run = get_object_or_404(BacktestRun, pk=pk, owner=request.user)
    run.tags = parse_tags(request.POST.get("tags", ""))
    run.save(update_fields=["tags"])
    return _back(request, pk)


@require_POST
def template_delete(request, pk):
    get_object_or_404(RunTemplate, pk=pk, owner=request.user).delete()
    messages.success(request, "Vorlage gelöscht.")
    return redirect("index")
