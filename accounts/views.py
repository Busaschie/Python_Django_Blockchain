import logging
import os

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth import get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import PasswordChangeForm, PasswordResetForm
from django.core.mail import EmailMultiAlternatives
from django.core import signing
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from config.i18n import mail_text

from . import demo, throttle
from .forms import DeleteAccountForm, EmailChangeForm, EmailForm, PasswordSetForm

User = get_user_model()


def _send(subject, body, to):
    try:
        send_mail(mail_text(subject), mail_text(body), None, [to])
        return True
    except Exception:
        logging.getLogger(__name__).exception("E-Mail-Versand an %s fehlgeschlagen", to)
        return False


SALT = "accounts.register"
SALT_EMAIL = "accounts.email-change"
MAX_AGE = 60 * 60 * 24   # Link 24 h gültig


@login_not_required
def register(request):
    """Schritt 1: E-Mail eingeben -> Bestätigungslink per Mail. Es wird noch kein Konto angelegt."""
    if request.user.is_authenticated:
        return redirect("index")
    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        if throttle.blocked(request, "register", email):
            form.add_error(None, throttle.MESSAGE["register"])
            return render(request, "accounts/register.html", {"form": form}, status=429)
        throttle.record(request, "register", email)
        # Gleiche Antwort in beiden Fällen (keine Konten erratbar); die Mail unterscheidet sich.
        if User.objects.filter(username__iexact=email).exists():
            subject, body = "Du hast bereits ein Konto", render_to_string(
                "accounts/exists_email.txt", {"login": request.build_absolute_uri(reverse("login")),
                                              "forgot": request.build_absolute_uri(reverse("forgot"))})
        else:
            link = request.build_absolute_uri(reverse("register_confirm", args=[signing.dumps(email, salt=SALT)]))
            subject, body = "Bestätige deine E-Mail-Adresse", render_to_string("accounts/confirm_email.txt", {"link": link})
        if not _send(subject, body, email):
            form.add_error(None, "Die E-Mail konnte nicht gesendet werden. Bitte später erneut versuchen.")
            return render(request, "accounts/register.html", {"form": form})
        request.session["register_email"] = email
        return redirect("register_sent")
    return render(request, "accounts/register.html", {"form": form})


@login_not_required
def register_sent(request):
    return render(request, "accounts/register_sent.html", {"email": request.session.get("register_email", "")})


@login_not_required
def register_confirm(request, token):
    """Schritt 2: Link geklickt -> Passwort festlegen -> erst jetzt wird das Konto angelegt."""
    try:
        email = signing.loads(token, salt=SALT, max_age=MAX_AGE)
    except signing.SignatureExpired:
        return render(request, "accounts/register_invalid.html", {"expired": True}, status=400)
    except signing.BadSignature:
        return render(request, "accounts/register_invalid.html", status=400)
    if User.objects.filter(username__iexact=email).exists():   # Link bereits benutzt
        return render(request, "accounts/register_invalid.html", {"used": True}, status=400)
    form = PasswordSetForm(request.POST or None, email=email)
    if request.method == "POST" and form.is_valid():
        user = User.objects.create_user(username=email, email=email, password=form.cleaned_data["password1"])
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        request.session.pop("register_email", None)
        return redirect("index")
    return render(request, "accounts/register_confirm.html", {"form": form, "email": email})


def _account_page(request, pw=None, em=None, de=None):
    return render(request, "accounts/account.html", {
        "form": pw or PasswordChangeForm(request.user),
        "email_form": em or EmailChangeForm(request.user),
        "delete_form": de or DeleteAccountForm(request.user)})


def account(request):
    if demo.is_demo(request.user):       # Demo: nur Erklärung statt der Formulare
        return render(request, "accounts/account.html", {"is_demo_page": True})
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)   # angemeldet bleiben
        messages.success(request, "Passwort geändert.")
        return redirect("account")
    return _account_page(request, pw=form) if request.method == "POST" else _account_page(request)


@require_POST
@demo.forbid
def email_change(request):
    """E-Mail ändern: neue Adresse wird erst nach Klick auf den Link übernommen."""
    form = EmailChangeForm(request.user, request.POST)
    if not form.is_valid():
        return _account_page(request, em=form)
    new = form.cleaned_data["new_email"]
    link = request.build_absolute_uri(reverse("email_change_confirm", args=[
        signing.dumps({"u": request.user.pk, "e": new}, salt=SALT_EMAIL)]))
    if not _send("Neue E-Mail-Adresse bestätigen", render_to_string("accounts/email_change_mail.txt", {"link": link}), new):
        form.add_error(None, "Die E-Mail konnte nicht gesendet werden. Bitte später erneut versuchen.")
        return _account_page(request, em=form)
    messages.success(request, f"Bestätigungslink an {new} gesendet. Die Adresse ändert sich erst nach dem Klick darauf.")
    return redirect("account")


def email_change_confirm(request, token):
    try:
        data = signing.loads(token, salt=SALT_EMAIL, max_age=MAX_AGE)
    except signing.BadSignature:   # inkl. abgelaufen
        messages.error(request, "Der Link ist ungültig oder abgelaufen.")
        return redirect("account")
    if data.get("u") != request.user.pk:
        messages.error(request, "Der Link gehört zu einem anderen Konto. Bitte mit diesem Konto anmelden.")
        return redirect("account")
    new = data["e"]
    if User.objects.filter(username__iexact=new).exclude(pk=request.user.pk).exists():
        messages.error(request, "Diese E-Mail-Adresse ist inzwischen vergeben.")
        return redirect("account")
    request.user.username = request.user.email = new
    request.user.save(update_fields=["username", "email"])
    messages.success(request, f"E-Mail geändert. Anmeldung ab jetzt mit {new}.")
    return redirect("account")


@require_POST
@demo.forbid
def account_delete(request):
    """Konto samt aller Auswertungen endgültig löschen (Passwort + Bestätigung nötig)."""
    form = DeleteAccountForm(request.user, request.POST)
    if not form.is_valid():
        return _account_page(request, de=form)
    request.user.delete()   # CASCADE: Auswertungen, KI-Aufrufe
    logout(request)
    messages.success(request, "Dein Konto und alle Auswertungen wurden gelöscht.")
    return redirect("login")


class ThrottledLoginView(auth_views.LoginView):
    """Sperrt nach zu vielen Fehlversuchen (pro Konto und pro IP), ohne das Passwort noch zu prüfen."""

    def post(self, request, *a, **kw):
        ident = request.POST.get("username", "").strip().lower()
        if throttle.blocked(request, "login", ident):
            form = self.get_form_class()(request=request)   # ungebunden: keine Passwortprüfung
            form.cleaned_data = {}
            form.add_error(None, throttle.MESSAGE["login"])
            return self.render_to_response(self.get_context_data(form=form), status=429)
        return super().post(request, *a, **kw)

    def form_invalid(self, form):
        throttle.record(self.request, "login", self.request.POST.get("username", "").strip().lower())
        return super().form_invalid(form)

    def form_valid(self, form):
        throttle.clear("login", form.cleaned_data["username"])
        return super().form_valid(form)


class _ResetForm(PasswordResetForm):
    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email, html_email_template_name=None):
        subject = mail_text("".join(render_to_string(subject_template_name, context).splitlines()))
        body = mail_text(render_to_string(email_template_name, context))
        EmailMultiAlternatives(subject, body, from_email, [to_email]).send()


class ThrottledPasswordResetView(auth_views.PasswordResetView):
    form_class = _ResetForm

    def form_valid(self, form):
        email = form.cleaned_data["email"].strip().lower()
        if throttle.blocked(self.request, "forgot", email):
            form.add_error(None, throttle.MESSAGE["forgot"])
            return self.render_to_response(self.get_context_data(form=form), status=429)
        throttle.record(self.request, "forgot", email)
        return super().form_valid(form)


@login_not_required
def legal(request, page):
    """Impressum / Datenschutzerklärung. Angaben des Betreibers kommen aus Umgebungsvariablen (LEGAL_*)."""
    keys = ("NAME", "STREET", "CITY", "EMAIL", "PHONE")
    info = {k.lower(): os.environ.get(f"LEGAL_{k}", "").strip() for k in keys}
    info["missing"] = [k for k in ("NAME", "STREET", "CITY", "EMAIL") if not info[k.lower()]]
    suffix = "_en" if getattr(request, "lang", "de") == "en" else ""     # englische Fassung der Rechtstexte
    return render(request, f"accounts/{page}{suffix}.html", {"l": info})


@login_not_required
def admin_login(request, **kwargs):
    """Admin-Login mit Bremse gegen Passwort-Raten (gleiche Zaehler wie die normale Anmeldung, strengere Grenzen).
    Gesperrt wird nach zu vielen Fehlversuchen je Konto und je IP; erfolgreiche Anmeldung setzt den Zaehler zurueck."""
    from django.contrib import admin
    from django.http import HttpResponse
    ident = request.POST.get("username", "").strip().lower() if request.method == "POST" else ""
    if request.method == "POST" and throttle.blocked(request, "admin", ident):
        resp = HttpResponse(throttle.MESSAGE["admin"], status=429, content_type="text/plain; charset=utf-8")
        resp["Retry-After"] = str(throttle.retry_after("admin"))
        return resp
    resp = admin.site.login(request, **kwargs)
    if request.method == "POST":
        if resp.status_code in (301, 302):
            throttle.clear("admin", ident)
        else:
            throttle.record(request, "admin", ident)
    return resp


@require_POST
def account_export(request):
    """Datenauskunft/-uebertragbarkeit (Art. 15, 20 DSGVO): alle eigenen Daten als JSON-Download.
    Enthalten: Konto, Auswertungen (Einstellungen, Kennzahlen, Trades, Tags, KI-Kommentar), Vorlagen, Paper-Konten mit Journal,
    gespeicherte KI-Antworten. Nicht enthalten: Passwort-Hash und die (grossen) Kursreihen der Diagramme."""
    import json

    from django.http import HttpResponse
    from django.utils import timezone

    from backtester.models import AiResult, BacktestRun, PaperAccount, RunTemplate
    u = request.user
    data = {
        "exportiert_am": timezone.now().isoformat(),
        "konto": {"benutzername": u.username, "email": u.email, "angelegt": u.date_joined.isoformat()},
        "auswertungen": [{
            "id": r.pk, "angelegt": r.created_at.isoformat(), "chain": r.chain, "symbol": r.symbol, "zeitfenster": r.timeframe,
            "strategie": r.strategy, "parameter": r.params, "modus": r.mode, "von": str(r.start_date), "bis": str(r.end_date),
            "gebuehr": r.fee, "slippage": r.slippage, "quelle": r.source, "boerse": r.exchange, "ausfuehrung": r.execution,
            "status": r.status, "kennzahlen": r.metrics, "validierung": r.validation, "einstellungen": r.job,
            "trades": (r.curves or {}).get("trades", []), "favorit": r.favorite, "tags": r.tag_list,
            "kommentar": r.ai_comment, "oeffentlicher_link_aktiv": bool(r.share_token),
        } for r in BacktestRun.objects.filter(owner=u).order_by("pk")],
        "vorlagen": [{"name": t.name, "einstellungen": t.job} for t in RunTemplate.objects.filter(owner=u)],
        "paper_konten": [{
            "name": a.name, "start": a.created_at.isoformat(), "startkapital": a.start_capital, "kontowert": a.equity(),
            "strategie": a.strategy, "parameter": a.params, "aktiv": a.active,
            "journal": [{"kerze": e.candle, "seite": e.side, "kurs": e.price, "menge": e.units, "kosten": e.cost_paid,
                         "kontowert_danach": e.equity_after, "trade_pct": e.ret_pct} for e in a.entries.all()],
        } for a in PaperAccount.objects.filter(owner=u)],
        "ki_antworten": [{"art": a.kind, "lauf": a.run_id, "schluessel": a.key, "antwort": a.payload} for a in AiResult.objects.filter(owner=u)],
    }
    resp = HttpResponse(json.dumps(data, ensure_ascii=False, indent=1, default=str), content_type="application/json; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="meine-daten.json"'
    return resp


_DEMO_PAGES = {
    "disabled": (503, "Die Demo ist zurzeit abgeschaltet."),
    "invalid": (403, "Dieser Demo-Link ist ungültig oder abgelaufen. Demo-Links sind 48 Stunden gültig."),
    "full": (503, "Zurzeit sind alle Demo-Plätze belegt. Bitte in einigen Minuten erneut versuchen."),
    "throttled": (429, "Zu viele Demo-Zugänge von dieser Adresse. Bitte in einer Stunde erneut versuchen."),
}


def _demo_page(request, kind):
    status, text = _DEMO_PAGES[kind]
    resp = render(request, "accounts/demo_info.html", {"text": text, "kind": kind}, status=status)
    if status in (429, 503):
        resp["Retry-After"] = "3600" if status == 429 else "600"
    return resp


@login_not_required
def demo_login(request, token):
    """Demo-Link: legt ein eigenes, begrenztes Demo-Konto an und meldet es an."""
    if not demo.enabled():
        return _demo_page(request, "disabled")
    if not demo.valid_token(token):
        return _demo_page(request, "invalid")
    if request.user.is_authenticated:
        if not demo.is_demo(request.user):
            messages.info(request, "Du bist mit deinem eigenen Konto angemeldet. Für die Demo bitte abmelden oder ein privates Fenster nutzen.")
        return redirect("index")             # ein vorhandenes Demo-Konto wird weiterverwendet
    if throttle.blocked(request, "demo", ""):
        return _demo_page(request, "throttled")
    demo.cleanup()
    if demo.active_count() >= demo.MAX_ACCOUNTS:
        return _demo_page(request, "full")
    user = demo.create_user()
    throttle.record(request, "demo", "")
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    request.session.set_expiry(demo.ACCOUNT_HOURS * 3600)
    return redirect("index")


def demo_link(request):
    """Nur für Admins: erzeugt einen frischen Demo-Link (2 Tage gültig) zum Kopieren."""
    from django.http import Http404
    if not request.user.is_staff:
        raise Http404
    link = request.build_absolute_uri(reverse("demo_login", args=[demo.make_token()]))
    return render(request, "accounts/demo_link.html", {
        "link": link, "hours": demo.TOKEN_MAX_AGE // 3600, "account_hours": demo.ACCOUNT_HOURS, "max_accounts": demo.MAX_ACCOUNTS,
        "max_runs": demo.MAX_RUNS, "ai_limit": demo.AI_LIMIT, "active": demo.active_count(), "enabled": demo.enabled()})
