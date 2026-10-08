from django.conf import settings
from django.db import models

from .chains import CHAIN_CHOICES, EXCHANGES
from .strategies import LABELS


class BacktestRun(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.CASCADE,
                              related_name="runs")
    chain = models.CharField(max_length=3, choices=CHAIN_CHOICES, default="btc")
    symbol = models.CharField(max_length=20)
    timeframe = models.CharField(max_length=5)
    strategy = models.CharField(max_length=30)
    params = models.JSONField(default=dict)
    days = models.PositiveIntegerField()  # Anzahl Kalendertage im Zeitraum
    start_date = models.DateField(null=True, blank=True)
    end_date = models.DateField(null=True, blank=True)
    fee = models.FloatField(default=0.001)
    slippage = models.FloatField(default=0.0)  # je Seite, zusaetzlich zur Gebuehr
    source = models.CharField(max_length=10, default="ccxt")
    execution = models.CharField(max_length=5, default="close")
    metrics = models.JSONField(default=dict)
    curves = models.JSONField(default=dict)
    validation = models.JSONField(default=dict, blank=True)
    exchange = models.CharField(max_length=20, default="binance")
    indicator_lib = models.CharField(max_length=20, blank=True, default="")  # TA-Lib oder pandas-Ersatz
    data_note = models.CharField(max_length=200, blank=True, default="")
    # Hintergrund-Berechnung: Eingaben (job) und Status
    job = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, default="done")  # queued | running | done | error
    error = models.TextField(blank=True, default="")
    batch = models.CharField(max_length=12, blank=True, default="", db_index=True)  # Chain-Vergleich
    created_at = models.DateTimeField(auto_now_add=True)
    # KI-Kommentar (nur auf Knopfdruck, einmal je Lauf gespeichert)
    ai_comment = models.JSONField(default=dict, blank=True)
    ai_source = models.CharField(max_length=60, blank=True, default="")   # z. B. "groq:openai/gpt-oss-120b" oder "regeln"
    ai_created = models.DateTimeField(null=True, blank=True)
    # Aktuelles Signal: letzter Stand (position 1/0, seit, Kurs, geprueft) und Wunsch nach Mail bei Wechsel
    signal_alert = models.BooleanField(default=False)
    signal_state = models.JSONField(default=dict, blank=True)
    # Teilen: leer = nicht geteilt; sonst ist die Auswertung ueber /geteilt/<Token>/ ohne Anmeldung lesbar
    share_token = models.CharField(max_length=40, blank=True, default="", db_index=True)
    # Ordnung: Favorit und Tags (als "|a|b|" gespeichert, damit sich per Textsuche exakt filtern laesst)
    favorite = models.BooleanField(default=False)
    tags = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]

    @property
    def tag_list(self):
        return [t for t in self.tags.split("|") if t]

    @property
    def strategy_label(self):
        return LABELS.get(self.strategy, self.strategy)

    @property
    def mode(self):
        if self.job:
            return self.job.get("mode", "single")
        return self.validation.get("kind", "single") if self.validation else "single"

    @property
    def period_label(self):
        if self.start_date and self.end_date:
            return f"{self.start_date:%d.%m.%Y} – {self.end_date:%d.%m.%Y} ({self.days} Tage)"
        return f"{self.days} Tage"

    @property
    def source_label(self):
        return "Synthetisch" if self.source == "synthetic" else self.exchange_label

    @property
    def risk_label(self):
        """Kurzbeschreibung der Risiko-Einstellungen aus dem Job (leer = keine)."""
        j, parts = self.job or {}, []
        if j.get("stop_loss"):
            parts.append(f"Stop-Loss {j['stop_loss']:g} %")
        if j.get("take_profit"):
            parts.append(f"Take-Profit {j['take_profit']:g} %")
        if j.get("trailing_stop"):
            parts.append(f"Trailing-Stop {j['trailing_stop']:g} %")
        mode, val = j.get("size_mode", "full"), j.get("size_value")
        if mode == "fixed" and val:
            parts.append(f"Größe {val:g} % des Kapitals")
        elif mode == "vol" and val:
            parts.append(f"Volatilitätsziel {val:g} % p. a.")
        return " · ".join(parts)

    @property
    def exchange_label(self):
        return EXCHANGES.get(self.exchange, self.exchange)

    @property
    def status_label(self):
        return {"queued": "wartet", "running": "läuft", "done": "fertig", "error": "Fehler"}[self.status]

    @property
    def is_pending(self):
        return self.status in ("queued", "running")

    @property
    def mode_label(self):
        return {"single": "Einzellauf", "split": "Train/Test", "walkforward": "Walk-Forward"}[self.mode]

    @property
    def execution_label(self):
        return {"open": "Eröffnungskurs der Folgekerze", "close": "Schlusskurs der Signalkerze"}[self.execution]

    def __str__(self):
        return f"{self.strategy_label} {self.symbol} {self.timeframe}"


class RunTemplate(models.Model):
    """Gespeicherte Lieblingskonfiguration: alle Formularwerte, der Zeitraum als Laenge in Tagen (bis heute)."""
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="run_templates")
    name = models.CharField(max_length=60)
    job = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [models.UniqueConstraint(fields=["owner", "name"], name="uniq_template_name_per_owner")]

    @property
    def summary(self):
        from .strategies import LABELS
        j = self.job or {}
        return " · ".join(str(x) for x in (LABELS.get(j.get("strategy"), j.get("strategy")), j.get("timeframe"),
                                           {"single": "Einzellauf", "split": "Train/Test", "walkforward": "Walk-Forward"}.get(j.get("mode"))) if x)

    def __str__(self):
        return self.name


class Candle(models.Model):
    """Abgeschlossene Kerze einer Börse (Cache, damit die Börsen-API nicht bei jedem Lauf belastet wird)."""
    exchange = models.CharField(max_length=20)
    symbol = models.CharField(max_length=20)
    timeframe = models.CharField(max_length=5)
    ts = models.BigIntegerField()  # Kerzenbeginn, Millisekunden seit 1970 (UTC)
    open = models.FloatField()
    high = models.FloatField()
    low = models.FloatField()
    close = models.FloatField()
    volume = models.FloatField()

    class Meta:
        constraints = [models.UniqueConstraint(fields=["exchange", "symbol", "timeframe", "ts"],
                                               name="uniq_candle")]


class CandleCoverage(models.Model):
    """Welcher lückenlose Zeitraum je Börse/Paar/Zeitfenster bereits im Cache liegt."""
    exchange = models.CharField(max_length=20)
    symbol = models.CharField(max_length=20)
    timeframe = models.CharField(max_length=5)
    covered_from = models.BigIntegerField()          # ms, einschließlich
    covered_to = models.BigIntegerField()            # ms, ausschließlich (Ende der letzten Kerze)
    floor_ts = models.BigIntegerField(null=True, blank=True)  # davor gibt es bei der Börse keine Daten
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["exchange", "symbol", "timeframe"],
                                               name="uniq_candle_coverage")]


class ExchangeBlock(models.Model):
    """Sperre einer Börse (IP-Sperre/Limit): bis dahin werden keine Anfragen mehr gesendet."""
    exchange = models.CharField(max_length=20, unique=True)
    until = models.DateTimeField()
    reason = models.CharField(max_length=200, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def exchange_label(self):
        return EXCHANGES.get(self.exchange, self.exchange)


class AiCall(models.Model):
    """Jeder Aufruf des KI-Anbieters (Tageslimits je Benutzer und gesamt). Bleibt auch nach dem Loeschen eines Laufs."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="ai_calls")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    model = models.CharField(max_length=60, blank=True, default="")
    ok = models.BooleanField(default=False)
