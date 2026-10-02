from django.db import models

from .chains import CHAIN_CHOICES, EXCHANGES
from .strategies import LABELS


class BacktestRun(models.Model):
    chain = models.CharField(max_length=3, choices=CHAIN_CHOICES, default="btc")
    symbol = models.CharField(max_length=20)
    timeframe = models.CharField(max_length=5)
    strategy = models.CharField(max_length=30)
    params = models.JSONField(default=dict)
    days = models.PositiveIntegerField()
    fee = models.FloatField(default=0.001)
    slippage = models.FloatField(default=0.0)  # je Seite, zusaetzlich zur Gebuehr
    source = models.CharField(max_length=10, default="ccxt")
    execution = models.CharField(max_length=5, default="close")
    metrics = models.JSONField(default=dict)
    curves = models.JSONField(default=dict)
    validation = models.JSONField(default=dict, blank=True)
    exchange = models.CharField(max_length=20, default="binance")
    data_note = models.CharField(max_length=200, blank=True, default="")
    # Hintergrund-Berechnung: Eingaben (job) und Status
    job = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, default="done")  # queued | running | done | error
    error = models.TextField(blank=True, default="")
    batch = models.CharField(max_length=12, blank=True, default="", db_index=True)  # Chain-Vergleich
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    @property
    def strategy_label(self):
        return LABELS.get(self.strategy, self.strategy)

    @property
    def mode(self):
        if self.job:
            return self.job.get("mode", "single")
        return self.validation.get("kind", "single") if self.validation else "single"

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
