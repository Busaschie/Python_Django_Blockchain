from django.db import models
from django.utils import timezone


class Attempt(models.Model):
    """Zähler für die Anmelde-/Mail-Bremse (liegt in der DB, damit es über mehrere Worker hinweg gilt)."""
    scope = models.CharField(max_length=20)    # login | register | forgot
    key = models.CharField(max_length=320)     # z. B. "ip:1.2.3.4" oder "user:a@b.de"
    created = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["scope", "key", "created"])]
