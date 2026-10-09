from django.core.management.base import BaseCommand

from accounts import demo


class Command(BaseCommand):
    help = "Löscht abgelaufene Demo-Konten samt Läufen."

    def handle(self, *a, **kw):
        self.stdout.write(f"{demo.cleanup()} abgelaufene Demo-Konten gelöscht.")
