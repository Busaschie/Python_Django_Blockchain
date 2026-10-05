"""Fehlende Migrationen anwenden. Wird einmalig vom Gunicorn-Master (gunicorn.conf.py)
bzw. vor `runserver` (manage.py) aufgerufen - nicht mehr beim Import in jedem Worker."""
import logging
import os
import time

log = logging.getLogger("tradebot.migrate")


def enabled(default: bool) -> bool:
    flag = os.environ.get("AUTO_MIGRATE")
    return flag.lower() in ("1", "true", "yes") if flag is not None else default


def run() -> None:
    import django
    from django.core.management import call_command
    django.setup()
    collect_static()
    from django.db import connections
    for attempt in range(1, 4):
        try:
            call_command("migrate", interactive=False, verbosity=1)
            log.info("Datenbank ist aktuell.")
            break
        except Exception:
            log.exception("Migration fehlgeschlagen (Versuch %s von 3)", attempt)
            time.sleep(3)
    ensure_admin()
    connections.close_all()


def ensure_admin() -> None:
    """Admin-Konto aus ADMIN_EMAIL/ADMIN_PASSWORD anlegen (Render hat in der Gratis-Stufe keine Shell).
    Existiert die E-Mail schon, wird sie nur zum Superuser gemacht - das Passwort bleibt unveraendert."""
    email, pw = os.environ.get("ADMIN_EMAIL", "").strip().lower(), os.environ.get("ADMIN_PASSWORD", "")
    if not email:
        return
    try:
        from django.contrib.auth import get_user_model
        U = get_user_model()
        u = U.objects.filter(username__iexact=email).first()
        if u is None:
            if not pw:
                log.warning("ADMIN_EMAIL gesetzt, aber ADMIN_PASSWORD fehlt - kein Admin angelegt.")
                return
            U.objects.create_superuser(username=email, email=email, password=pw)
            log.info("Admin-Konto %s angelegt.", email)
        elif not (u.is_staff and u.is_superuser):
            u.is_staff = u.is_superuser = True
            u.save(update_fields=["is_staff", "is_superuser"])
            log.info("%s ist jetzt Admin.", email)
    except Exception:
        log.exception("Admin-Konto konnte nicht angelegt werden")


def collect_static() -> None:
    from django.core.management import call_command
    try:
        call_command("collectstatic", interactive=False, verbosity=0)
    except Exception:
        log.exception("collectstatic fehlgeschlagen")
