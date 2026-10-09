"""Demo-Zugang: ein geheimer, 2 Tage gültiger Link legt je Besuch ein eigenes, begrenztes Demo-Konto an.

- Fester Link: /demo/ (läuft nie ab, für Bewerbungsseiten). Die GET-Seite erklärt nur; erst ein Klick (POST) legt das Konto an, damit
  Suchmaschinen und Link-Vorschauen keine Plätze belegen.
- Einladungslink: signierter Token (Django `signing`, Ablauf nach TOKEN_MAX_AGE) unter /demo/<token>/, erzeugt per `manage.py demo_link`
  oder auf /demo-link/ (nur Admin). Er legt das Konto sofort an.
- Konto: Benutzername `demo-<zufall>@demo.invalid`, kein Passwort, keine E-Mail. Es läuft nach ACCOUNT_HOURS ab und wird dann samt Daten gelöscht.
- Grenzen: höchstens MAX_ACCOUNTS gleichzeitig AKTIVE Demo-Konten (ein Konto zählt nur, solange es innerhalb der letzten IDLE_MINUTES benutzt
  wurde), insgesamt höchstens MAX_TOTAL Demo-Konten, MAX_RUNS Läufe je Konto, AI_LIMIT KI-Aufrufe je Konto (insgesamt, nicht je Tag).
- Gesperrt: Passwort/E-Mail ändern, Konto löschen, Teilen, Signal-Mail, Paper-Trading starten (die Seiten sind sichtbar, mit Erklärung).
- Kill-Schalter: Umgebungsvariable DEMO_ENABLED=0 schaltet alle Demo-Links ab.
"""
import os
import secrets
from datetime import timedelta
from functools import wraps

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.core import signing
from django.shortcuts import redirect
from django.utils import timezone

SALT = "tradebot-demo-link"
TOKEN_MAX_AGE = 2 * 24 * 3600     # Link gilt 2 Tage
ACCOUNT_HOURS = 48                # Lebensdauer eines Demo-Kontos: danach sind Eingaben und Läufe gelöscht
MAX_ACCOUNTS = 5                  # gleichzeitig aktive Demo-Konten
IDLE_MINUTES = 60                 # ein Konto ohne Aktivität in dieser Zeit gibt seinen Platz frei (seine Daten bleiben bis zum Ablauf)
MAX_TOTAL = 20                    # vorhandene (nicht abgelaufene) Demo-Konten insgesamt: schützt die Datenbank
TOUCH_MINUTES = 5                 # so selten wird die Aktivität gespeichert
MAX_RUNS = 20                     # Läufe je Demo-Konto
AI_LIMIT = 5                      # KI-Aufrufe je Demo-Konto (insgesamt)
DOMAIN = "demo.invalid"           # reservierte Domain: kann nie echte Mails empfangen

BLOCKED = "Im Demo-Konto nicht möglich."

User = get_user_model()


def enabled() -> bool:
    return os.environ.get("DEMO_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


def make_token() -> str:
    return signing.dumps({"n": secrets.token_hex(4)}, salt=SALT)


def valid_token(token: str) -> bool:
    try:
        signing.loads(token, salt=SALT, max_age=TOKEN_MAX_AGE)
        return True
    except signing.BadSignature:      # inkl. abgelaufen
        return False


def is_demo(user) -> bool:
    return bool(user is not None and getattr(user, "is_authenticated", False) and user.username.endswith("@" + DOMAIN)
                and not user.is_staff and not user.has_usable_password())


def expires_at(user):
    return user.date_joined + timedelta(hours=ACCOUNT_HOURS)


def is_active(user) -> bool:
    return timezone.now() < expires_at(user)


def hours_left(user) -> int:
    secs = (expires_at(user) - timezone.now()).total_seconds()
    return max(0, int(secs // 3600) + (1 if secs % 3600 else 0))


def _demo_users():
    return User.objects.filter(username__endswith="@" + DOMAIN, is_staff=False, password__startswith="!")


def _alive():
    return _demo_users().filter(date_joined__gte=timezone.now() - timedelta(hours=ACCOUNT_HOURS))


def active_count() -> int:
    """Demo-Konten, die gerade benutzt werden (Aktivität innerhalb IDLE_MINUTES)."""
    return _alive().filter(last_login__gte=timezone.now() - timedelta(minutes=IDLE_MINUTES)).count()


def total_count() -> int:
    return _alive().count()


def has_room() -> bool:
    return active_count() < MAX_ACCOUNTS and total_count() < MAX_TOTAL


def touch(user) -> None:
    """Aktivität festhalten (höchstens alle TOUCH_MINUTES ein Schreibzugriff)."""
    now = timezone.now()
    if user.last_login is None or now - user.last_login > timedelta(minutes=TOUCH_MINUTES):
        User.objects.filter(pk=user.pk).update(last_login=now)
        user.last_login = now


def cleanup() -> int:
    """Abgelaufene Demo-Konten samt Läufen löschen (CASCADE). Gibt die Zahl der gelöschten Konten zurück."""
    old = _demo_users().filter(date_joined__lt=timezone.now() - timedelta(hours=ACCOUNT_HOURS))
    n = old.count()
    for u in old:
        u.delete()
    return n


def create_user():
    return User.objects.create_user(f"demo-{secrets.token_hex(4)}@{DOMAIN}", email="", password=None)


def runs_used(user) -> int:
    from backtester.models import BacktestRun
    return BacktestRun.objects.filter(owner=user).count()


def runs_free(user) -> int:
    return max(0, MAX_RUNS - runs_used(user))


def ai_used(user) -> int:
    from backtester.models import AiCall
    return AiCall.objects.filter(user=user).count()


def ai_left(user) -> int:
    return max(0, AI_LIMIT - ai_used(user))


def forbid(view):
    """Dekorator: Demo-Konten dürfen diese Aktion nicht, sie landen mit Erklärung auf der Konto-Seite."""
    @wraps(view)
    def wrapper(request, *a, **kw):
        if is_demo(request.user):
            messages.error(request, BLOCKED + " Passwort, E-Mail und Konto lassen sich nur mit einem eigenen Konto ändern.")
            return redirect("account")
        return view(request, *a, **kw)
    return wrapper
