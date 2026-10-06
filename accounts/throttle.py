"""Einfache Bremse gegen Passwort-Raten und Mail-Spam. Zählt Versuche pro IP und pro Konto/Adresse."""
from datetime import timedelta

from django.utils import timezone

from .models import Attempt

# scope -> [(Schlüsselart, Limit, Zeitfenster in Sekunden)]
RULES = {
    "login":    [("user", 5, 900), ("ip", 20, 900)],
    "register": [("email", 3, 3600), ("ip", 10, 3600)],
    "forgot":   [("email", 3, 3600), ("ip", 10, 3600)],
}
MESSAGE = {
    "login": "Zu viele Fehlversuche. Bitte in 15 Minuten erneut versuchen.",
    "register": "Zu viele Anfragen. Bitte in einer Stunde erneut versuchen.",
    "forgot": "Zu viele Anfragen. Bitte in einer Stunde erneut versuchen.",
}


def client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return (xff.split(",")[0].strip() if xff else request.META.get("REMOTE_ADDR", "")) or "?"


def _keys(request, scope, ident):
    kinds = {"ip": client_ip(request), "user": ident, "email": ident}
    return [(kind, f"{kind}:{kinds[kind]}", limit, win) for kind, limit, win in RULES[scope]]


def blocked(request, scope, ident):
    now = timezone.now()
    for _kind, key, limit, win in _keys(request, scope, ident):
        if Attempt.objects.filter(scope=scope, key=key, created__gte=now - timedelta(seconds=win)).count() >= limit:
            return True
    return False


def record(request, scope, ident):
    now = timezone.now()
    Attempt.objects.bulk_create([Attempt(scope=scope, key=key) for _k, key, _l, _w in _keys(request, scope, ident)])
    Attempt.objects.filter(created__lt=now - timedelta(days=1)).delete()   # aufräumen


def clear(scope, ident):
    Attempt.objects.filter(scope=scope, key=f"user:{ident}").delete()
