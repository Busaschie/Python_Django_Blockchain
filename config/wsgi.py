import logging
import os
import time

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()

log = logging.getLogger("tradebot.migrate")
LOCK_ID = 727365  # beliebige feste Zahl fuer die Postgres-Sperre


def auto_migrate() -> None:
    """Fehlende Datenbank-Migrationen beim Start anwenden (Standard auf Render, sonst AUTO_MIGRATE=1).

    Mehrere Gunicorn-Worker starten gleichzeitig: Eine Postgres-Sperre sorgt dafuer, dass nur einer
    migriert, die anderen warten und finden danach nichts mehr zu tun."""
    from django.conf import settings
    flag = os.environ.get("AUTO_MIGRATE")
    if not (flag.lower() in ("1", "true", "yes") if flag is not None else settings.ON_RENDER):
        return
    from django.core.management import call_command
    from django.db import connection, connections, transaction
    from django.db.migrations.executor import MigrationExecutor

    def migrate_pending():
        executor = MigrationExecutor(connection)
        plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
        if plan:
            call_command("migrate", interactive=False, verbosity=0)
            log.info("Migrationen angewendet: %s", ", ".join(f"{m.app_label}.{m.name}" for m, _ in plan))
        else:
            log.info("Datenbank ist aktuell, keine Migration nötig.")

    for attempt in range(1, 4):
        try:
            if connection.vendor == "postgresql":
                with transaction.atomic():  # Sperre gilt bis zum Ende dieser Transaktion
                    with connection.cursor() as cur:
                        cur.execute("SELECT pg_advisory_xact_lock(%s)", [LOCK_ID])
                    migrate_pending()
            else:  # SQLite kann Schemaaenderungen nicht in einer Transaktion
                migrate_pending()
            break
        except Exception:
            log.exception("Migration fehlgeschlagen (Versuch %s von 3)", attempt)
            time.sleep(3)
    connections.close_all()


auto_migrate()
