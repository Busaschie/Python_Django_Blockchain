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
    from django.db import connections
    for attempt in range(1, 4):
        try:
            call_command("migrate", interactive=False, verbosity=1)
            log.info("Datenbank ist aktuell.")
            break
        except Exception:
            log.exception("Migration fehlgeschlagen (Versuch %s von 3)", attempt)
            time.sleep(3)
    connections.close_all()
