#!/usr/bin/env python
import os, sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    if len(sys.argv) > 1 and sys.argv[1] == "runserver" and os.environ.get("RUN_MAIN") != "true":
        from config import automigrate   # lokal: fehlende Migrationen vor dem Start anwenden
        if automigrate.enabled(True):
            automigrate.run()
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)
