import logging

from django.contrib import messages
from django.contrib.auth import get_user_model, login, update_session_auth_hash
from django.contrib.auth.decorators import login_not_required
from django.contrib.auth.forms import PasswordChangeForm
from django.core import signing
from django.core.mail import send_mail
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse

from .forms import EmailForm, PasswordSetForm

User = get_user_model()


SALT = "accounts.register"
MAX_AGE = 60 * 60 * 24   # Link 24 h gültig


@login_not_required
def register(request):
    """Schritt 1: E-Mail eingeben -> Bestätigungslink per Mail. Es wird noch kein Konto angelegt."""
    if request.user.is_authenticated:
        return redirect("index")
    form = EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        email = form.cleaned_data["email"]
        if User.objects.filter(username__iexact=email).exists():
            pass   # nichts senden; gleiche Antwort, damit keine Konten erraten werden können
        else:
            link = request.build_absolute_uri(reverse("register_confirm", args=[signing.dumps(email, salt=SALT)]))
            try:
                send_mail("Bestätige deine E-Mail-Adresse",
                          render_to_string("accounts/confirm_email.txt", {"link": link}),
                          None, [email])
            except Exception:
                logging.getLogger(__name__).exception("Bestätigungsmail fehlgeschlagen")
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


def account(request):
    form = PasswordChangeForm(request.user, request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)   # angemeldet bleiben
        messages.success(request, "Passwort geändert.")
        return redirect("account")
    return render(request, "accounts/account.html", {"form": form})
