import os
from pathlib import Path
from urllib.parse import urlparse

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# --- Umgebung (lokal: Standardwerte, auf Render: Umgebungsvariablen) ---
#SECRET_KEY = os.environ.get("SECRET_KEY", "dev-only-change-me")
#DEBUG = os.environ.get("DEBUG", "1").lower() in ("1", "true", "yes")
RENDER_HOST = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")  # setzt Render automatisch

if os.environ.get("ALLOWED_HOSTS"):
    ALLOWED_HOSTS = [h.strip() for h in os.environ["ALLOWED_HOSTS"].split(",") if h.strip()]
elif RENDER_HOST:
    ALLOWED_HOSTS = [RENDER_HOST, "localhost", "127.0.0.1"]
else:
    ALLOWED_HOSTS = ["*"]
if RENDER_HOST:  # hinter dem Render-Proxy: HTTPS erkennen, Formulare (CSRF) zulassen
    CSRF_TRUSTED_ORIGINS = [f"https://{RENDER_HOST}"]
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

INSTALLED_APPS = [
    "django.contrib.admin", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "backtester",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
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
