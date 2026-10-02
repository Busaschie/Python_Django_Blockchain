"""Preisdaten laden: ccxt (echte Börsendaten) oder synthetisch (offline testen)."""
import zlib

import numpy as np
import pandas as pd

from .chains import EXCHANGE_LIMITS, EXCHANGES

TIMEFRAME_MS = {"15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
PERIODS_PER_YEAR = {"15m": 35_040, "1h": 8_760, "4h": 2_190, "1d": 365}


def fetch_ohlcv(symbol: str, timeframe: str, days: int, source: str = "ccxt",
                exchange: str = "binance"):
    """Liefert (DataFrame, info). info = {"symbol": tatsaechlich genutztes Paar, "note": Hinweis}."""
    if source == "synthetic":
        return _synthetic(symbol, timeframe, days), {"symbol": symbol, "note": ""}

    import ccxt
    if exchange not in EXCHANGES:
        raise ValueError(f"Unbekannte Börse: {exchange}")
    name = EXCHANGES[exchange]
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    tfs = ex.timeframes or {}
    if tfs and timeframe not in tfs:
        raise ValueError(f"{name} bietet das Zeitfenster {timeframe} nicht an.")

    # Handelspaar aufloesen: USDT bevorzugt, sonst USD/USDC (nicht jede Boerse listet USDT-Paare)
    ex.load_markets()
    base = symbol.split("/")[0]
    sym = next((c for q in ("USDT", "USD", "USDC")
                if (c := f"{base}/{q}") in ex.markets and ex.markets[c].get("active") is not False), None)
    if sym is None:
        raise ValueError(f"{base} wird auf {name} weder gegen USDT noch gegen USD/USDC gehandelt.")

    limit, now = EXCHANGE_LIMITS.get(exchange, 500), ex.milliseconds()
    since, rows = now - days * 86_400_000, []
    for _ in range(500):  # Sicherung gegen Endlosschleifen
        batch = ex.fetch_ohlcv(sym, timeframe, since=since, limit=limit)
        new = [r for r in batch if not rows or r[0] > rows[-1][0]]
        if not new:
            break
        rows += new
        since = rows[-1][0] + TIMEFRAME_MS[timeframe]
        if since >= now:
            break
    if len(rows) < 100:
        raise ValueError(f"{name} lieferte für {sym} ({timeframe}) nur {len(rows)} Kerzen - zu wenig für einen Backtest.")

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["ts"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").set_index("ts")
    got = (df.index[-1] - df.index[0]).days
    note = f"{name} lieferte nur {got} von {days} angeforderten Tagen." if got < days * 0.9 else ""
    return df, {"symbol": sym, "note": note}


START_PRICE = {"BTC": 60_000, "ETH": 3_000, "SOL": 150}


def _synthetic(symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    n = max(int(days * 86_400_000 / TIMEFRAME_MS[timeframe]), 50)
    rng = np.random.default_rng(zlib.crc32(symbol.encode()))  # je Symbol reproduzierbar
    close = START_PRICE.get(symbol.split("/")[0], 100) * np.exp(np.cumsum(rng.normal(0.0002, 0.01, n)))
    prev_close = np.concatenate([[close[0]], close[:-1]])
    open_ = prev_close * (1 + rng.normal(0, 0.002, n))  # kleine Luecke zum Vortagesschluss
    idx = pd.date_range(end=pd.Timestamp.now(tz="UTC").floor("h"), periods=n,
                        freq=pd.Timedelta(milliseconds=TIMEFRAME_MS[timeframe]))
    return pd.DataFrame({"open": open_, "high": np.maximum(open_, close) * 1.003,
                         "low": np.minimum(open_, close) * 0.997, "close": close,
                         "volume": 1.0}, index=idx)
