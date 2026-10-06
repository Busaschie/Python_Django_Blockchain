import logging
import os

from django.contrib import messages
from django.contrib.auth import views as auth_views
from django.contrib.auth import get_user_model, login, logout, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core import signing
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import throttle
from .forms import DeleteAccountForm, EmailChangeForm, EmailForm, PasswordSetForm

User = get_user_model()


def _send(subject, body, to):
    try:
        send_mail(subject, body, None, [to])
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
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)   # angemeldet bleiben
        messages.success(request, "Passwort geändert.")
        return redirect("account")
    return _account_page(request, pw=form) if request.method == "POST" else _account_page(request)


@require_POST
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


class ThrottledPasswordResetView(auth_views.PasswordResetView):
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
    return render(request, f"accounts/{page}.html", {"l": info})
