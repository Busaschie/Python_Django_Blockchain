"""Börsendaten mit Cache, Sperren-Schutz und Binance-Archiv.

Reihenfolge je Zeitraum: 1. Cache (Datenbank), 2. Binance-Archiv (nur Binance, keine API),
3. Börsen-API (nur für das, was fehlt). Wird eine Sperre erkannt (HTTP 418/429), sendet die App
bis zu deren Ende keine Anfragen mehr: weitere Versuche würden die Sperre verlängern.
"""
import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from django.db import transaction

from . import binance_archive
from .chains import EXCHANGE_LIMITS, EXCHANGES
from .models import Candle, CandleCoverage, ExchangeBlock

log = logging.getLogger("tradebot.marketdata")
QUOTES = ("USDT", "USD", "USDC")
MARKETS_TTL = 12 * 3600
BAN_RE = re.compile(r"banned until (\d{10,13})")


class ExchangeBlocked(Exception):
    """Die Börse hat die IP gesperrt oder ein Limit gemeldet; es werden keine Anfragen gesendet."""

    def __init__(self, message: str, short: str = ""):
        super().__init__(message)
        self.short = short or message


# --- Zustand je Prozess ---------------------------------------------------------------------
_guard = threading.Lock()
_clients, _ex_locks, _key_locks, _markets = {}, {}, {}, {}


def reset_state():  # für Tests
    with _guard:
        _clients.clear(); _ex_locks.clear(); _key_locks.clear(); _markets.clear()


def _client(exchange: str):
    import ccxt
    with _guard:  # ein Client je Prozess: gemeinsame Drosselung (rateLimit) für alle Threads
        if exchange not in _clients:
            _clients[exchange] = getattr(ccxt, exchange)({"enableRateLimit": True})
        return _clients[exchange]


def _lock_for(store: dict, key):
    with _guard:
        return store.setdefault(key, threading.Lock())


# --- Sperren ---------------------------------------------------------------------------------
def active_block(exchange: str):
    row = ExchangeBlock.objects.filter(exchange=exchange).first()
    return row if row and row.until > datetime.now(timezone.utc) else None


def _blocked_error(exchange: str, row) -> ExchangeBlocked:
    name = EXCHANGES[exchange]
    return ExchangeBlocked(
        f"{name} hat den Server (IP-Adresse) bis {row.until:%d.%m.%Y %H:%M} UTC gesperrt: {row.reason}. "
        "Bis dahin werden keine Anfragen gesendet, damit sich die Sperre nicht verlängert. Bereits "
        "geladene Kerzen kommen aus dem Cache; sonst bitte eine andere Börse wählen.",
        short=f"{name} gesperrt bis {row.until:%d.%m. %H:%M} UTC")


def set_block(exchange: str, until: datetime, reason: str) -> None:
    with transaction.atomic():
        row = ExchangeBlock.objects.select_for_update().filter(exchange=exchange).first()
        if row is None:
            ExchangeBlock.objects.create(exchange=exchange, until=until, reason=reason[:200])
        elif until > row.until or row.until <= datetime.now(timezone.utc):
            row.until, row.reason = until, reason[:200]
            row.save()


def classify(exc: Exception, now: datetime):
    """(gesperrt bis, Grund) oder None, wenn die Meldung keine Sperre ist."""
    import ccxt
    text = str(exc)
    m = BAN_RE.search(text)
    if m:  # Binance: "IP banned until <ms>"
        v = int(m.group(1))
        return (datetime.fromtimestamp(v / 1000 if v > 10 ** 11 else v, timezone.utc) + timedelta(seconds=5),
                "zu viele Anfragen (IP-Sperre durch die Börse)")
    if "restricted location" in text.lower() or " 451" in text:
        return now + timedelta(hours=1), "Zugriff von diesem Standort nicht erlaubt (Regionssperre)"
    if "418" in text:
        return now + timedelta(minutes=10), "zu viele Anfragen (IP-Sperre, Dauer unbekannt)"
    if isinstance(exc, ccxt.RateLimitExceeded) or "429" in text:
        return now + timedelta(minutes=2), "Anfragelimit erreicht"
    if isinstance(exc, ccxt.DDoSProtection):
        return now + timedelta(minutes=5), "Schutzmechanismus der Börse (zu viele Anfragen)"
    return None


def _rest(exchange: str, fn):
    """Börsen-Aufruf: Sperre prüfen, Aufrufe je Börse nacheinander, Sperren erkennen und merken."""
    import ccxt
    row = active_block(exchange)
    if row:
        raise _blocked_error(exchange, row)
    with _lock_for(_ex_locks, exchange):
        row = active_block(exchange)  # inzwischen von einem anderen Thread gemeldet?
        if row:
            raise _blocked_error(exchange, row)
        try:
            return fn()
        except (ccxt.DDoSProtection, ccxt.RateLimitExceeded, ccxt.ExchangeNotAvailable) as exc:
            verdict = classify(exc, datetime.now(timezone.utc))
            if verdict is None:
                raise
            until, reason = verdict
            set_block(exchange, until, reason)
            log.error("%s: Sperre erkannt (%s), keine Anfragen bis %s UTC. Meldung: %s",
                      exchange, reason, until, str(exc)[:300])
            raise _blocked_error(exchange, active_block(exchange)) from exc


# --- Handelspaar -----------------------------------------------------------------------------
def markets_for(exchange: str):
    ts, mk = _markets.get(exchange, (0, None))
    if mk is not None and time.time() - ts < MARKETS_TTL:
        return mk
    client = _client(exchange)
    mk = _rest(exchange, client.load_markets)
    _markets[exchange] = (time.time(), mk)
    return mk


def resolve_symbol(exchange: str, base: str, timeframe: str) -> str:
    for q in QUOTES:  # 1. aus dem Cache bekannt: kein Börsen-Aufruf
        s = f"{base}/{q}"
        if CandleCoverage.objects.filter(exchange=exchange, symbol=s, timeframe=timeframe).exists():
            return s
    if exchange == "binance":  # Archiv und API führen USDT-Paare; den schweren Märkte-Abruf sparen
        return f"{base}/USDT"
    markets = markets_for(exchange)  # einmal je Prozess und 12 h
    sym = next((c for q in QUOTES
                if (c := f"{base}/{q}") in markets and markets[c].get("active") is not False), None)
    if sym is None:
        raise ValueError(f"{base} wird auf {EXCHANGES[exchange]} weder gegen USDT noch gegen USD/USDC gehandelt.")
    return sym


# --- Laden und Zwischenspeichern ----------------------------------------------------------------
def _fetch_rest(exchange, sym, timeframe, tf, x, y):
    """Kerzen aus [x, y) per API. Rückgabe: (Zeilen, done_to, Sperre oder None)."""
    client, limit = _client(exchange), EXCHANGE_LIMITS.get(exchange, 500)
    since, rows, done_to = x, {}, x
    for _ in range(2000):  # Sicherung gegen Endlosschleifen
        try:
            batch = _rest(exchange, lambda: client.fetch_ohlcv(sym, timeframe, since=since, limit=limit))
        except ExchangeBlocked as blocked:
            return _finish(rows, tf, x, y), (max(r[0] for r in rows.values()) + tf if rows else x), blocked
        new = [r for r in batch if r[0] not in rows and r[0] >= since]
        if not new:
            done_to = y
            break
        for r in new:
            rows[r[0]] = r
        since = max(rows) + tf
        done_to = min(since, y)
        if since >= y:
            break
    return _finish(rows, tf, x, y), done_to, None


def _finish(rows, tf, x, y):
    return sorted((r for r in rows.values() if x <= r[0] and r[0] + tf <= y))


def _fill(exchange, sym, timeframe, tf, x, y, stats, now_ms):
    rows, warns, done_to = {}, [], x
    if exchange == "binance":
        try:
            r, done_to = binance_archive.fetch_range(sym.replace("/", ""), timeframe, tf, x, y, now_ms)
            rows.update({k[0]: k for k in r})
            stats["archive"] += len(r)
        except binance_archive.ArchiveError as exc:
            log.warning("Binance-Archiv nicht erreichbar, nutze die API: %s", exc)
            done_to = x
    if done_to < y:
        r, done_to, blocked = _fetch_rest(exchange, sym, timeframe, tf, done_to, y)
        added = {k[0]: k for k in r if k[0] not in rows}
        rows.update(added)
        stats["api"] += len(added)
        if blocked:
            warns.append(blocked)
    return sorted(rows.values()), done_to, warns


def _store(exchange, sym, timeframe, rows):
    Candle.objects.bulk_create(
        [Candle(exchange=exchange, symbol=sym, timeframe=timeframe, ts=r[0], open=r[1], high=r[2],
                low=r[3], close=r[4], volume=r[5]) for r in rows],
        batch_size=2000, ignore_conflicts=True)


def _update_coverage(exchange, sym, timeframe, tf, kind, x, y, rows, done_to):
    first_ts = rows[0][0] if rows else None
    last_end = rows[-1][0] + tf if rows else None
    with transaction.atomic():
        cov = CandleCoverage.objects.select_for_update().filter(
            exchange=exchange, symbol=sym, timeframe=timeframe).first()
        if cov is None:
            if not rows:
                return  # nichts bekommen: keine Abdeckung behaupten
            late = first_ts > x + 2 * tf  # Daten beginnen erst später (vor Listing / begrenzte Historie)
            CandleCoverage.objects.create(
                exchange=exchange, symbol=sym, timeframe=timeframe,
                covered_from=first_ts if late else x, covered_to=max(done_to, last_end),
                floor_ts=first_ts if late else None)
        elif kind == "right":
            if rows:  # eine leere Antwort für mehrere Kerzen ist verdächtig: nicht als abgedeckt markieren
                cov.covered_to = max(cov.covered_to, done_to, last_end)
                cov.save()
        elif done_to >= y:  # "left": nur wenn der ganze Bereich abgeholt wurde
            if rows:
                late = first_ts > x + 2 * tf
                cov.covered_from = first_ts if late else x
                cov.floor_ts = first_ts if late else cov.floor_ts
            else:
                cov.floor_ts = cov.covered_from  # davor gibt es nichts
            cov.save()


def _ensure(exchange, sym, timeframe, tf, a, b, now_ms):
    stats, warns = {"archive": 0, "api": 0}, []
    cov = CandleCoverage.objects.filter(exchange=exchange, symbol=sym, timeframe=timeframe).first()
    if cov is None:
        plan = [("init", a, b)]
    else:
        lo = max(a, cov.floor_ts) if cov.floor_ts is not None else a
        plan = []
        if lo < cov.covered_from:
            plan.append(("left", lo, cov.covered_from))
        if b > cov.covered_to:
            plan.append(("right", cov.covered_to, b))
    for kind, x, y in plan:
        rows, done_to, w = _fill(exchange, sym, timeframe, tf, x, y, stats, now_ms)
        warns += w
        _store(exchange, sym, timeframe, rows)
        _update_coverage(exchange, sym, timeframe, tf, kind, x, y, rows, done_to)
    return stats, warns


def _load(exchange, sym, timeframe, tf, a, b) -> pd.DataFrame:
    qs = (Candle.objects.filter(exchange=exchange, symbol=sym, timeframe=timeframe, ts__gte=a, ts__lte=b - tf)
          .order_by("ts").values_list("ts", "open", "high", "low", "close", "volume"))
    df = pd.DataFrame.from_records(list(qs), columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    return df.set_index("ts")


def get_candles(exchange: str, base: str, timeframe: str, start, end, now_ms: int = None):
    """(DataFrame, info) für start..end (beide einschließlich), nur abgeschlossene Kerzen."""
    from .data import MIN_CANDLES, TIMEFRAME_MS, period_ms
    name = EXCHANGES[exchange]
    tfs = _client(exchange).timeframes or {}
    if tfs and timeframe not in tfs:
        raise ValueError(f"{name} bietet das Zeitfenster {timeframe} nicht an.")
    now_ms = now_ms or int(time.time() * 1000)
    tf = TIMEFRAME_MS[timeframe]
    a, end_ms = period_ms(start, end, now_ms)
    b = end_ms // tf * tf  # Ende der letzten abgeschlossenen Kerze
    if b <= a:
        raise ValueError("Der Zeitraum enthält noch keine abgeschlossene Kerze.")
    sym = resolve_symbol(exchange, base, timeframe)
    with _lock_for(_key_locks, (exchange, sym, timeframe)):  # gleiche Daten nicht doppelt laden
        stats, warns = _ensure(exchange, sym, timeframe, tf, a, b, now_ms)
    df = _load(exchange, sym, timeframe, tf, a, b)

    if len(df) < MIN_CANDLES:
        if warns:
            raise warns[0]
        raise ValueError(f"{name} lieferte für {sym} ({timeframe}) von {start:%d.%m.%Y} bis {end:%d.%m.%Y} "
                         f"nur {len(df)} abgeschlossene Kerzen (mindestens {MIN_CANDLES} nötig).")
    requested = (end - start).days + 1
    got = (df.index[-1] - df.index[0]).days + 1
    if warns and got < requested * 0.9:  # große Lücke durch die Sperre: abbrechen statt verkürzt auswerten
        raise warns[0]
    note = ""
    if got < requested * 0.9:
        note = f"{name} lieferte nur {got} von {requested} angeforderten Tagen (Daten ab {df.index[0]:%d.%m.%Y})."
    if warns:
        note = (note + " " if note else "") + f"Daten enden am {df.index[-1]:%d.%m.%Y}: {warns[0].short}."
    cache = max(0, len(df) - stats["archive"] - stats["api"])
    log.info("%s %s %s: %d Kerzen (Cache %d, Archiv %d, API %d)", name, sym, timeframe, len(df),
             cache, stats["archive"], stats["api"])
    return df, {"symbol": sym, "note": note[:200]}
