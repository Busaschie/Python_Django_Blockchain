"""E-Mail-Versand per HTTPS-API (Port 443). Noetig, weil Render bei kostenlosen Diensten ausgehendes SMTP
(Ports 25/465/587) sperrt. Unterstuetzt Brevo und Resend; Konfiguration ueber Umgebungsvariablen."""
import json
import os
import urllib.error
import urllib.request
from email.utils import parseaddr

from django.core.mail.backends.base import BaseEmailBackend


def _post(url, headers, payload):
    req = urllib.request.Request(url, json.dumps(payload).encode(), {"Content-Type": "application/json",
                                 "User-Agent": "tradebot/1.0", **headers})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Mail-API {e.code}: {e.read().decode(errors='replace')[:300]}") from None


class ApiEmailBackend(BaseEmailBackend):
    def send_messages(self, messages):
        provider = os.environ.get("EMAIL_API", "brevo").lower()
        key = os.environ["EMAIL_API_KEY"]
        sent = 0
        for m in messages:
            name, addr = parseaddr(m.from_email)
            if provider == "resend":
                _post("https://api.resend.com/emails", {"Authorization": f"Bearer {key}"},
                      {"from": m.from_email, "to": list(m.to), "subject": m.subject, "text": m.body})
            else:
                _post("https://api.brevo.com/v3/smtp/email", {"api-key": key},
                      {"sender": {"email": addr, **({"name": name} if name else {})},
                       "to": [{"email": t} for t in m.to], "subject": m.subject, "textContent": m.body})
            sent += 1
        return sent
