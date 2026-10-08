"""Performance: kleine Zwischenspeicher und optional paralleles Rechnen großer Raster.

Zwischenspeicher (nur im Arbeitsspeicher des Servers, gehen beim Neustart verloren, ändern nie ein Ergebnis):
- OHLCV-Cache:    dieselbe Kursanfrage kurz nacheinander wird nicht erneut aus Datenbank/Börse geladen.
- Raster-Cache:   die Grid-Search (Train/Test, Walk-Forward) ist eine reine Funktion von Daten und Einstellungen.
- Metrik-Cache:   Kennzahlen eines Parameterpunkts (Kosten-Sensitivität, Parameter-Stabilität).
Der Schlüssel enthält immer *alle* Eingaben (inkl. eines Fingerabdrucks der Kursdaten), ein Treffer liefert also
exakt dieselbe Zahl wie eine Neuberechnung.

Parallel rechnen (optional): `PARALLEL_WORKERS=<n>` (Standard 1 = aus). Die Kerze-für-Kerze-Simulation mit Stops
ist reines Python und deshalb nicht mit Threads beschleunigbar; große Raster laufen daher in einem Prozess-Pool
(Start-Methode "spawn", sicher neben Threads/Datenbankverbindungen). Jeder Worker braucht Arbeitsspeicher
(ca. 100 MB), deshalb ist die Funktion standardmäßig aus (Render Free: 512 MB). Schlägt der Pool fehl,
wird automatisch seriell weitergerechnet.
"""
import atexit
import copy
import hashlib
import json
import multiprocessing as mp
import os
import threading
import time
from collections import OrderedDict
from concurrent.futures import ProcessPoolExecutor

MIN_PARALLEL_ITEMS = 8   # darunter lohnt der Pool nicht


class TTLCache:
    """Kleiner LRU-Cache mit Ablaufzeit; threadsicher. Werte werden beim Speichern und Lesen kopiert."""

    def __init__(self, maxsize: int, ttl: float):
        self.maxsize, self.ttl = maxsize, ttl
        self._d: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        self.hits = self.misses = 0

    def get(self, key):
        with self._lock:
            item = self._d.get(key)
            if item is not None and time.monotonic() - item[0] <= self.ttl:
                self._d.move_to_end(key)
                self.hits += 1
                return copy.deepcopy(item[1])
            if item is not None:
                del self._d[key]
            self.misses += 1
            return None

    def set(self, key, value):
        with self._lock:
            self._d[key] = (time.monotonic(), copy.deepcopy(value))
            self._d.move_to_end(key)
            while len(self._d) > self.maxsize:
                self._d.popitem(last=False)

    def clear(self):
        with self._lock:
            self._d.clear()
            self.hits = self.misses = 0

    def __len__(self):
        return len(self._d)


ohlcv_cache = TTLCache(maxsize=4, ttl=600)          # DataFrames: wenige, kurz
grid_cache = TTLCache(maxsize=64, ttl=3600)
metrics_cache = TTLCache(maxsize=512, ttl=3600)


def clear_all():
    for c in (ohlcv_cache, grid_cache, metrics_cache):
        c.clear()


def fingerprint(df) -> str:
    """Fingerabdruck der Kursdaten (Zeitstempel + OHLC): gleiche Daten -> gleicher Wert."""
    h = hashlib.blake2b(digest_size=12)
    h.update(df.index.asi8.tobytes() if hasattr(df.index, "asi8") else repr(df.index).encode())
    for col in ("open", "high", "low", "close"):
        if col in df:
            h.update(df[col].to_numpy(dtype="float64").tobytes())
    return h.hexdigest()


def digest(obj) -> str:
    """Kurzer Hash beliebiger JSON-artiger Einstellungen (Parameter, Validierungsangaben)."""
    return hashlib.blake2b(json.dumps(obj, sort_keys=True, default=repr).encode(), digest_size=10).hexdigest()


# --------------------------------------------------------------------------------------
# Paralleles Rechnen
# --------------------------------------------------------------------------------------
_pool = None
_pool_lock = threading.Lock()


def workers() -> int:
    try:
        n = int(os.environ.get("PARALLEL_WORKERS", "1"))
    except ValueError:
        n = 1
    return max(1, min(n, os.cpu_count() or 1, 8))


def _get_pool(n: int):
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=n, mp_context=mp.get_context("spawn"))
        return _pool


def shutdown_pool():
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)


atexit.register(shutdown_pool)


def parallel_map(worker, common: tuple, items: list):
    """Rechnet `worker(*common, chunk)` über `items`, verteilt auf den Prozess-Pool.

    `worker` muss auf Modulebene definiert sein und je Eintrag des Chunks genau ein Ergebnis in derselben Reihenfolge
    liefern. Rückgabe: Ergebnisliste in der Reihenfolge von `items` - oder None, wenn nicht parallel gerechnet wurde
    (aus, zu wenig Arbeit oder Pool-Fehler): der Aufrufer rechnet dann seriell."""
    n = min(workers(), len(items))
    if n <= 1 or len(items) < MIN_PARALLEL_ITEMS:
        return None
    chunks = [items[k::n] for k in range(n)]          # abwechselnd verteilt: gleichmäßige Last
    try:
        pool = _get_pool(workers())
        parts = [f.result(timeout=600) for f in [pool.submit(worker, *common, c) for c in chunks]]
    except Exception:  # noqa: BLE001  (Pool defekt, Speicher knapp, nicht serialisierbar ...)
        shutdown_pool()
        return None
    out = [None] * len(items)
    for k, part in enumerate(parts):
        out[k::n] = part
    return out
