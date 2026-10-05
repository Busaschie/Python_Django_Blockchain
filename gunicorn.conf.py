# Gunicorn liest diese Datei automatisch. on_starting laeuft einmal im Master, bevor Worker starten.
import os


def on_starting(server):
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from config import automigrate
    from django.conf import settings
    if automigrate.enabled(settings.ON_RENDER):
        automigrate.run()
