"""Aktuelles Signal einer Auswertung und Mail bei Signal-Wechsel.

Die Strategie mit den Parametern des Laufs wird auf die zuletzt abgeschlossenen Kerzen angewendet. Angezeigt wird der
Zustand der letzten abgeschlossenen Kerze (long = investiert, flat = nicht investiert) und seit wann er gilt.
Stop-Loss, Take-Profit, Trailing-Stop und Positionsgroesse sind darin NICHT enthalten (sie haengen vom Einstieg ab).
Ein-/Ausschalten per Umgebungsvariable SIGNALS_ENABLED; die Pruefung fuer Mails startet ein externer Zeitplan
(siehe README: /signale/pruefen/ oder `manage.py check_signals`)."""
import logging
from datetime import datetime, timedelta

from django.conf import settings
from django.core.mail import send_mail
from django.urls import reverse
from django.utils import timezone

from .chains import CHAINS
from .data import MIN_CANDLES, fetch_ohlcv
from .strategies import STRATEGIES

log = logging.getLogger("tradebot.signals")
LOOKBACK_DAYS = {"15m": 10, "1h": 45, "4h": 150, "1d": 600}
COOLDOWN = timedelta(seconds=60)


def enabled(user=None) -> bool:
    """Schalter SIGNALS_ENABLED; Demo-Konten sehen die Seiten immer (ihre Aktionen sind gesperrt), damit die Funktion erklärt werden kann."""
    if getattr(settings, "SIGNALS_ENABLED", False):
        return True
    from accounts import demo
    return demo.is_demo(user)


def current(run, cache=None) -> dict:
    """Aktueller Zustand: {"ok", "position", "since", "price", "candle", "checked"} oder {"ok": False, "reason"}."""
    if run.source == "synthetic":
        return {"ok": False, "reason": "bei synthetischen Daten gibt es kein aktuelles Signal"}
    end = timezone.now().date()
    key = (run.chain, run.timeframe, run.exchange, run.source)
    if cache is not None and key in cache:
        df = cache[key]
    else:
        df, _ = fetch_ohlcv(CHAINS[run.chain]["symbol"], run.timeframe, end - timedelta(days=LOOKBACK_DAYS[run.timeframe]),
                            end, run.source, run.exchange)
        if cache is not None:
            cache[key] = df
    if len(df) < MIN_CANDLES:
        return {"ok": False, "reason": "zu wenige aktuelle Kursdaten"}
    func, _ = STRATEGIES[run.strategy]
    sig = func(df, **run.params).astype(int)
    pos = int(sig.iloc[-1])
    changed = sig.ne(sig.shift()).to_numpy()
    last_change = max((i for i in range(len(sig)) if changed[i]), default=0)
    since = sig.index[last_change] if last_change > 0 else None   # None: gilt schon seit Beginn der geladenen Daten
    return {"ok": True, "position": pos, "price": round(float(df["close"].iloc[-1]), 6),
            "candle": sig.index[-1].isoformat(), "since": since.isoformat() if since is not None else None,
            "checked": timezone.now().isoformat()}


def label(pos) -> str:
    return "long (investiert)" if pos == 1 else "flat (nicht investiert)"


def refresh(run) -> dict:
    """Aktuellen Stand berechnen und im Lauf speichern (mit kurzer Sperre gegen Dauer-Klicks)."""
    prev = run.signal_state or {}
    if prev.get("checked") and timezone.now() - datetime.fromisoformat(prev["checked"]) < COOLDOWN:
        return prev
    st = current(run)
    if st["ok"]:
        run.signal_state = st
        run.save(update_fields=["signal_state"])
    return st


def _mail(run, old, new, st, base_url) -> None:
    body = (f"Signal-Wechsel bei deiner Auswertung {run.get_chain_display()} · {run.strategy_label} · {run.timeframe}:\n\n"
            f"vorher: {label(old)}\njetzt:  {label(new)}\nKerze:  {st['candle'][:16].replace('T', ' ')} UTC, Schlusskurs {st['price']}\n\n"
            f"Das ist der Zustand der Strategie auf historischen Regeln, keine Anlageberatung. Stop-Loss, Take-Profit und "
            f"Positionsgröße sind darin nicht berücksichtigt.\n\nAuswertung: {base_url}{reverse('detail', args=[run.pk])}\n"
            f"Die Signal-Mail kannst du dort jederzeit ausschalten.\n")
    send_mail(f"Signal-Wechsel: {run.get_chain_display()} {label(new).split()[0]}", body, None, [run.owner.email])


def check_all(base_url="") -> dict:
    """Alle Läufe mit aktivierter Signal-Mail prüfen und bei Wechsel mailen. Fehler einzelner Läufe stoppen nichts."""
    from .models import BacktestRun
    cache, checked, sent, errors = {}, 0, 0, 0
    for run in BacktestRun.objects.filter(signal_alert=True, status="done").select_related("owner"):
        if not run.owner or not run.owner.email or not run.owner.is_active:
            continue
        try:
            st = current(run, cache)
            if not st["ok"]:
                continue
            checked += 1
            old = (run.signal_state or {}).get("position")
            if old is not None and old != st["position"]:
                _mail(run, old, st["position"], st, base_url)
                sent += 1
            run.signal_state = st
            run.save(update_fields=["signal_state"])
        except Exception:  # noqa: BLE001
            errors += 1
            log.exception("Signal-Prüfung für Lauf %s fehlgeschlagen", run.pk)
    return {"checked": checked, "sent": sent, "errors": errors}
