import os

from django.core.management.base import BaseCommand

from accounts import demo


class Command(BaseCommand):
    help = "Erzeugt einen Demo-Link (2 Tage gültig). Muss mit demselben SECRET_KEY laufen wie die Seite (z. B. in der Render-Shell)."

    def add_arguments(self, parser):
        parser.add_argument("--host", default="", help="Basisadresse, z. B. https://meine-seite.onrender.com")

    def handle(self, *a, host="", **kw):
        base = (host or os.environ.get("DEMO_BASE_URL", "")).rstrip("/")
        if not base:
            h = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
            base = f"https://{h}" if h else "http://localhost:8000"
        self.stdout.write(f"{base}/demo/{demo.make_token()}/")
        self.stdout.write(f"Gültig {demo.TOKEN_MAX_AGE // 3600} Stunden; je Besuch ein eigenes Konto "
                          f"({demo.ACCOUNT_HOURS} Stunden, höchstens {demo.MAX_ACCOUNTS} gleichzeitig).")
