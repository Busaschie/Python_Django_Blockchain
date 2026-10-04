"""Kerzen aus dem öffentlichen Binance-Archiv (data.binance.vision).

Das sind Dateien auf einem CDN: kein API-Gewicht, keine IP-Sperre. Vergangene Monate liegen als
Monatsdatei vor, die letzten Wochen nur als Tagesdateien. Die API wird nur für die jüngsten Tage
gebraucht, die das Archiv noch nicht enthält.
"""
import io
import logging
import time
import zipfile
from datetime import datetime, timezone

import numpy as np
import pandas as pd

log = logging.getLogger("tradebot.archive")
BASE = "https://data.binance.vision/data/spot"
DAY_MS = 86_400_000
RECENT_DAYS = 40  # jüngere fehlende Monatsdateien: Tagesdateien versuchen


class ArchiveError(Exception):
    """Archiv nicht erreichbar (Netzwerk/Serverfehler), nicht: Datei existiert nicht."""


def monthly_url(sym: str, tf: str, year: int, month: int) -> str:
    return f"{BASE}/monthly/klines/{sym}/{tf}/{sym}-{tf}-{year}-{month:02d}.zip"


def daily_url(sym: str, tf: str, day_ms: int) -> str:
    d = datetime.fromtimestamp(day_ms / 1000, timezone.utc)
    return f"{BASE}/daily/klines/{sym}/{tf}/{sym}-{tf}-{d:%Y-%m-%d}.zip"


def http_get(url: str):
    """Dateiinhalt oder None, wenn es die Datei nicht gibt (404/403)."""
    import requests
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=(5, 60))
        except requests.RequestException as exc:
            if attempt == 2:
                raise ArchiveError(f"{url}: {exc}") from exc
            time.sleep(1 + attempt)
            continue
        if r.status_code == 200:
            return r.content
        if r.status_code in (403, 404):
            return None
        if attempt == 2:
            raise ArchiveError(f"{url}: HTTP {r.status_code}")
        time.sleep(1 + attempt)


def parse(content: bytes) -> list:
    """ZIP mit CSV -> [[ts_ms, open, high, low, close, volume], ...]"""
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        with z.open(z.namelist()[0]) as f:
            df = pd.read_csv(f, header=None, usecols=range(6), dtype=str)
    df = df.apply(pd.to_numeric, errors="coerce").dropna()  # entfernt eine evtl. Kopfzeile
    if df.empty:
        return []
    arr = df.to_numpy(dtype=float)
    ts = arr[:, 0].astype("int64")
    if ts.max() > 10 ** 14:  # Spot-Daten ab 2025 haben Mikrosekunden statt Millisekunden
        ts = ts // 1000
    return [[int(t), *map(float, row)] for t, row in zip(ts, arr[:, 1:])]


def _month_bounds(year: int, month: int):
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = datetime(year + (month == 12), month % 12 + 1, 1, tzinfo=timezone.utc)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def fetch_range(sym: str, timeframe: str, tf_ms: int, x: int, y: int, now_ms: int):
    """Kerzen aus [x, y) ms. Rückgabe: (Zeilen, done_to). done_to: bis wohin das Archiv lückenlos
    Auskunft gibt; ab dort muss die API übernehmen. sym ohne Schrägstrich, z. B. BTCUSDT."""
    rows, done_to = [], x
    first = datetime.fromtimestamp(x / 1000, timezone.utc)
    year, month = first.year, first.month
    recent_cut = now_ms - RECENT_DAYS * DAY_MS
    while True:
        m_start, m_end = _month_bounds(year, month)
        if m_start >= y:
            break
        stop = False
        if m_end <= now_ms:  # Monat abgeschlossen -> Monatsdatei
            data = http_get(monthly_url(sym, timeframe, year, month))
            if data is not None:
                rows += parse(data)
                done_to = max(done_to, min(m_end, y))
            elif m_end <= recent_cut:  # älterer Monat fehlt
                if rows:
                    break  # Loch mitten in den Daten: die API füllt ab hier
                done_to = max(done_to, min(m_end, y))  # vor dem Listing: es gibt nichts
            else:
                stop = _daily(sym, timeframe, max(m_start, x // DAY_MS * DAY_MS), min(m_end, y), now_ms, rows)
                done_to = max(done_to, rows_end(rows, done_to, tf_ms, y))
        else:  # laufender Monat -> Tagesdateien
            stop = _daily(sym, timeframe, max(m_start, x // DAY_MS * DAY_MS), min(m_end, y), now_ms, rows)
            done_to = max(done_to, rows_end(rows, done_to, tf_ms, y))
        if stop:
            break
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    kept = [r for r in rows if x <= r[0] and r[0] + tf_ms <= y]
    return kept, min(done_to, y)


def rows_end(rows, current, tf_ms, y):
    return min(y, (rows[-1][0] + tf_ms) if rows else current)


def _daily(sym, timeframe, day, limit, now_ms, rows) -> bool:
    """Tagesdateien ab `day` bis `limit`. True, wenn abgebrochen wurde (Archiv hinkt hinterher)."""
    while day < limit:
        if day + DAY_MS > now_ms:  # Tag noch nicht abgeschlossen
            return True
        data = http_get(daily_url(sym, timeframe, day))
        if data is None:
            return True
        rows += parse(data)
        day += DAY_MS
    return False
