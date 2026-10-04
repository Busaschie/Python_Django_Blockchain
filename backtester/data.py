"""Preisdaten laden: ccxt (echte Börsendaten) oder synthetisch (offline testen)."""
import time
import zlib
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from .chains import EXCHANGES

TIMEFRAME_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
DAY_MS = 86_400_000
MIN_CANDLES = 60  # darunter ist kein sinnvoller Backtest möglich
PERIODS_PER_YEAR = {"15m": 35_040, "1h": 8_760, "4h": 2_190, "1d": 365}


def _day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def period_ms(start: date, end: date, now_ms: int):
    """Zeitraum als [Start, Ende): das Enddatum ist eingeschlossen, höchstens bis jetzt."""
    return _day_ms(start), min(_day_ms(end) + DAY_MS, now_ms)


def fetch_ohlcv(symbol: str, timeframe: str, start: date, end: date, source: str = "ccxt",
                exchange: str = "binance"):
    """Liefert (DataFrame, info) für den Zeitraum start..end (beide einschließlich).
    info = {"symbol": tatsächlich genutztes Paar, "note": Hinweis}.
    Nur abgeschlossene Kerzen werden geliefert (die laufende Kerze zählt nicht)."""
    if source == "synthetic":
        return synthetic_ohlcv(symbol, timeframe, start, end), {"symbol": symbol, "note": ""}

    if exchange not in EXCHANGES:
        raise ValueError(f"Unbekannte Börse: {exchange}")
    from . import marketdata  # Cache, Sperren-Schutz, Binance-Archiv
    return marketdata.get_candles(exchange, symbol.split("/")[0], timeframe, start, end)


START_PRICE = {"BTC": 60_000, "ETH": 3_000, "SOL": 150}
ORIGIN_MS, HORIZON_MS = _day_ms(date(2010, 1, 1)), _day_ms(date(2031, 1, 1))


def synthetic_ohlcv(symbol: str, timeframe: str, start: date, end: date) -> pd.DataFrame:
    """Künstliche Kurse: je Symbol ein fester Zufallspfad ab 2010. Dasselbe Datum liefert immer
    denselben Kurs, egal welcher Zeitraum gewählt wird (wie bei echten Daten)."""
    start_ms, end_ms = period_ms(start, end, int(time.time() * 1000))
    step = TIMEFRAME_MS[timeframe]
    n, f = (HORIZON_MS - ORIGIN_MS) // step, step / DAY_MS  # f: Kerzenlänge in Tagen
    rng = np.random.default_rng(zlib.crc32(symbol.encode()))
    close = START_PRICE.get(symbol.split("/")[0], 100) * np.exp(
        np.cumsum(rng.normal(0.0002 * f, 0.01 * np.sqrt(f), n)))
    prev_close = np.concatenate([[close[0]], close[:-1]])
    open_ = prev_close * (1 + rng.normal(0, 0.002 * np.sqrt(f), n))  # kleine Lücke zum Vorschluss
    ts = ORIGIN_MS + np.arange(n) * step
    keep = (ts >= start_ms) & (ts + step <= end_ms)  # nur abgeschlossene Kerzen im Zeitraum
    idx = pd.to_datetime(ts[keep], unit="ms", utc=True)
    o, c = open_[keep], close[keep]
    return pd.DataFrame({"open": o, "high": np.maximum(o, c) * 1.003, "low": np.minimum(o, c) * 0.997,
                         "close": c, "volume": 1.0}, index=idx)
