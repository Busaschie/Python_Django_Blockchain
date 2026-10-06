from django.conf import settings
from django.core.management.base import BaseCommand

from backtester import signals


class Command(BaseCommand):
    help = "Prüft alle Läufe mit aktivierter Signal-Mail und sendet bei Signal-Wechsel eine E-Mail (z. B. als Render Cron Job)."

    def add_arguments(self, parser):
        parser.add_argument("--base-url", default="", help="z. B. https://meine-app.onrender.com (für den Link in der Mail)")

    def handle(self, *a, **o):
        if not signals.enabled():
            self.stdout.write("SIGNALS_ENABLED ist aus - nichts zu tun.")
            return
        self.stdout.write(str(signals.check_all(o["base_url"])))
