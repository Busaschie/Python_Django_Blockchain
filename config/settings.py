import hashlib
import os
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Umgebung: lokal Standardwerte, auf Render laeuft alles auch OHNE eigene Umgebungsvariablen ---
RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")   # setzt Render automatisch
ON_RENDER = bool(os.environ.get("RENDER") or RENDER_HOST)       # Render setzt RENDER=true

# DEBUG: aus, sobald die App auf Render laeuft. Ueberschreibbar mit DEBUG=1/0.
_debug = os.environ.get("DEBUG")
DEBUG = (not ON_RENDER) if _debug is None else _debug.lower() in ("1", "true", "yes")

# SECRET_KEY: aus der Umgebung, sonst auf Render stabil aus DATABASE_URL abgeleitet (gleicher Wert
# in allen Workern und nach Neustarts, aber nicht im Code), lokal ein Entwicklungswert.
SECRET_KEY = os.environ.get("SECRET_KEY") or (
    hashlib.sha256(f"tradebot-secret::{os.environ['DATABASE_URL']}".encode()).hexdigest()
    if ON_RENDER and os.environ.get("DATABASE_URL") else "dev-only-change-me")

if os.environ.get("ALLOWED_HOSTS"):
    ALLOWED_HOSTS = [h.strip() for h in os.environ["ALLOWED_HOSTS"].split(",") if h.strip()]
elif RENDER_HOST:
    ALLOWED_HOSTS = [RENDER_HOST, "localhost", "127.0.0.1"]
else:
    ALLOWED_HOSTS = ["*"]
if RENDER_HOST:  # hinter dem Render-Proxy: HTTPS erkennen, Formulare (CSRF) zulassen
    CSRF_TRUSTED_ORIGINS = [f"https://{RENDER_HOST}"]
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
if ON_RENDER and not DEBUG:
    SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = True

# Fehler (Tracebacks) in die Konsole, damit sie im Render-Log stehen (auch mit DEBUG=False)
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {"django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False}},
}

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "backtester",
    "accounts",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",   # alle Seiten nur angemeldet
    "django.contrib.messages.middleware.MessageMiddleware",
]
ROOT_URLCONF = "config.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "config.wsgi.application"

# --- Datenbank: DATABASE_URL (PostgreSQL, z. B. Neon), ohne Variable lokal SQLite ---
DATABASES = {"default": dj_database_url.config(
    default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
    conn_max_age=60,          # Verbindungen kurz wiederverwenden
    conn_health_checks=True,  # tote Verbindungen (z. B. nach Neon-Autosuspend) erkennen und ersetzen
)}
_db = DATABASES["default"]
if "postgresql" in _db["ENGINE"]:
    _db["DISABLE_SERVER_SIDE_CURSORS"] = True  # nötig für den Neon-Pooler (PgBouncer, Transaction-Modus)
    _opts = _db.setdefault("OPTIONS", {})
    _opts.setdefault("connect_timeout", 15)    # Neon kann nach Leerlauf einige Sekunden zum Aufwachen brauchen
    if _db.get("HOST") not in ("", "localhost", "127.0.0.1", None) and not str(_db.get("HOST")).startswith("/"):
        _opts.setdefault("sslmode", "require")  # Cloud-Datenbanken nur verschlüsselt
LANGUAGE_CODE = "de-de"
TIME_ZONE = "Europe/Berlin"
USE_TZ = True
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Anmeldung ---
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "index"
LOGOUT_REDIRECT_URL = "login"
# E-Mail-Versand (Passwort vergessen): SMTP ueber Umgebungsvariablen, sonst Ausgabe in der Konsole/im Log
if os.environ.get("EMAIL_API_KEY"):   # HTTPS-API (Brevo/Resend) - funktioniert auch dort, wo SMTP gesperrt ist (Render)
    EMAIL_BACKEND = "accounts.mailapi.ApiEmailBackend"
elif os.environ.get("EMAIL_HOST"):
    EMAIL_BACKEND = "django.core.mail.backends.smtp.EmailBackend"
    EMAIL_HOST = os.environ["EMAIL_HOST"]
    EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
    EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
    EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
    EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1").lower() in ("1", "true", "yes")
    EMAIL_TIMEOUT = 15
else:
    EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL") or os.environ.get("EMAIL_HOST_USER") or "noreply@tradebot.local"
