"""Strategien liefern eine Positionsreihe: 1 = long, 0 = flat (kein Shorting).
Alle Signale nutzen nur Daten bis zur aktuellen Kerze (kein Blick in die Zukunft)."""
import numpy as np
import pandas as pd

from . import indicators as ta


def sma_cross(df: pd.DataFrame, fast: int = 20, slow: int = 50) -> pd.Series:
    return (ta.sma(df["close"], fast) > ta.sma(df["close"], slow)).astype(int)


def rsi_reversion(df: pd.DataFrame, period: int = 14, low: int = 30, high: int = 70) -> pd.Series:
    r = ta.rsi(df["close"], period)
    sig = pd.Series(np.nan, index=df.index)
    sig[r < low] = 1   # ueberverkauft -> kaufen
    sig[r > high] = 0  # ueberkauft -> verkaufen
    return sig.ffill().fillna(0).astype(int)


def combo(df: pd.DataFrame, fast: int = 20, slow: int = 50, period: int = 14, entry: int = 40,
          exit: int = 70, logic: str = "trend") -> pd.Series:
    """Kombiniert (SMA + RSI).
    trend: Trendfilter + RSI-Einstieg. Kauf nur im Aufwaertstrend (SMA fast > slow) bei einem
           Ruecksetzer (RSI < entry). Verkauf bei RSI > exit oder wenn der Trend bricht.
    or:    ODER-Verknuepfung. Long, wenn der SMA-Crossover ODER die RSI-Strategie (Kauf RSI < entry,
           Verkauf RSI > exit) long ist."""
    close = df["close"]
    trend = ta.sma(close, fast) > ta.sma(close, slow)
    r = ta.rsi(close, period)
    sig = pd.Series(np.nan, index=df.index)
    if logic == "or":
        sig[r < entry] = 1
        sig[r > exit] = 0
        rsi_state = sig.ffill().fillna(0).astype(bool)
        return (trend | rsi_state).astype(int)
    sig[trend & (r < entry)] = 1
    sig[~trend | (r > exit)] = 0   # entry < exit: beide Bedingungen schliessen sich aus
    return sig.ffill().fillna(0).astype(int)


def bollinger(df: pd.DataFrame, period: int = 20, k: float = 2.0) -> pd.Series:
    """Bollinger-Rückkehr zum Mittelwert: Kauf, wenn der Schluss unter das untere Band fällt
    (Mittelwert - k Standardabweichungen); Verkauf, sobald er wieder über dem Mittelwert (SMA) schließt."""
    close = df["close"]
    mid = close.rolling(period).mean()
    lower = mid - k * close.rolling(period).std(ddof=0)
    sig = pd.Series(np.nan, index=df.index)
    sig[close < lower] = 1
    sig[close > mid] = 0
    return sig.ffill().fillna(0).astype(int)


def macd(df: pd.DataFrame, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.Series:
    """MACD: long, solange die MACD-Linie (EMA fast - EMA slow) über ihrer Signallinie (EMA davon) liegt."""
    line = ta.ema(df["close"], fast) - ta.ema(df["close"], slow)
    return (line > ta.ema(line, signal)).astype(int)


def donchian(df: pd.DataFrame, entry: int = 20, exit: int = 10) -> pd.Series:
    """Donchian-Ausbruch: Kauf bei Schluss über dem Hoch der letzten `entry` Kerzen, Verkauf bei Schluss
    unter dem Tief der letzten `exit` Kerzen (die aktuelle Kerze zählt nicht mit)."""
    close = df["close"]
    up = df["high"].rolling(entry).max().shift(1)
    down = df["low"].rolling(exit).min().shift(1)
    sig = pd.Series(np.nan, index=df.index)
    sig[close > up] = 1
    sig[close < down] = 0
    return sig.ffill().fillna(0).astype(int)


def momentum(df: pd.DataFrame, lookback: int = 30, threshold: float = 0.0) -> pd.Series:
    """Momentum: long, solange die Kursänderung über `lookback` Kerzen größer als `threshold` Prozent ist."""
    return (df["close"].pct_change(lookback) * 100 > threshold).astype(int)


LABELS = {"sma_cross": "SMA-Crossover", "rsi": "RSI", "combo": "Kombiniert (SMA + RSI)",
          "bollinger": "Bollinger-Bänder", "macd": "MACD", "donchian": "Donchian-Ausbruch", "momentum": "Momentum"}

STRATEGIES = {
    "sma_cross": (sma_cross, {"fast": 20, "slow": 50}),
    "rsi": (rsi_reversion, {"period": 14, "low": 30, "high": 70}),
    "combo": (combo, {"fast": 20, "slow": 50, "period": 14, "entry": 40, "exit": 70, "logic": "trend"}),
    "bollinger": (bollinger, {"period": 20, "k": 2.0}),
    "macd": (macd, {"fast": 12, "slow": 26, "signal": 9}),
    "donchian": (donchian, {"entry": 20, "exit": 10}),
    "momentum": (momentum, {"lookback": 30, "threshold": 0.0}),
}


# Suchraeume fuer die Grid-Search: x-/y-Achse + Funktion, die daraus Parameter baut
GRIDS = {
    "sma_cross": {
        "x": ("fast", [5, 10, 15, 20, 30, 40]),
        "y": ("slow", [30, 50, 80, 100, 150, 200]),
        "build": lambda fast, slow: {"fast": fast, "slow": slow} if fast < slow else None,
    },
    # Kombiniert: nur der SMA-Teil wird optimiert (36 statt ~576 Kombinationen: weniger Ueberanpassung);
    # der RSI-Teil und die Verknuepfung kommen als feste Werte aus dem Formular (siehe fixed_params).
    "combo": {
        "x": ("fast", [5, 10, 15, 20, 30, 40]),
        "y": ("slow", [30, 50, 80, 100, 150, 200]),
        "build": lambda fast, slow: {"fast": fast, "slow": slow} if fast < slow else None,
    },
    "rsi": {
        "x": ("period", [7, 10, 14, 21]),
        "y": ("low", [20, 25, 30, 35]),
        "build": lambda period, low: {"period": period, "low": low, "high": 100 - low},
    },
    "bollinger": {
        "x": ("period", [10, 15, 20, 30, 40]),
        "y": ("k", [1.5, 2.0, 2.5, 3.0]),
        "build": lambda period, k: {"period": period, "k": k},
    },
    "macd": {   # Signallinie fest 9
        "x": ("fast", [6, 8, 12, 16]),
        "y": ("slow", [20, 26, 35, 50]),
        "build": lambda fast, slow: {"fast": fast, "slow": slow, "signal": 9} if fast < slow else None,
    },
    "donchian": {
        "x": ("entry", [10, 20, 30, 55, 80]),
        "y": ("exit", [5, 10, 15, 20, 30]),
        "build": lambda entry, exit: {"entry": entry, "exit": exit} if exit <= entry else None,
    },
    "momentum": {
        "x": ("lookback", [10, 20, 30, 60, 90]),
        "y": ("threshold", [0.0, 2.0, 5.0, 10.0]),
        "build": lambda lookback, threshold: {"lookback": lookback, "threshold": threshold},
    },
}


def combo_extras(j) -> dict:
    """RSI-Teil und Verknuepfung der Kombi-Strategie aus Formular-/Jobwerten."""
    return {"period": j.get("rsi_period") or 14, "entry": j.get("rsi_entry") or 40,
            "exit": j.get("rsi_exit") or 70, "logic": j.get("combo_logic") or "trend"}


def fixed_params(strategy: str, j) -> dict:
    """Feste (nicht optimierte) Parameter einer Strategie fuer die Grid-Search."""
    return combo_extras(j) if strategy == "combo" else {}


def default_inputs(strategy: str) -> dict:
    """Standardwerte der Parameter-Felder je Strategie (fuer den Strategie-Vergleich im Einzellauf)."""
    if strategy == "rsi":
        return {"param_a": 14, "param_b": 30, "param_c": 70}
    if strategy == "bollinger":
        return {"param_a": 20, "param_b": 20, "param_c": None}      # Parameter 2 = Faktor in Zehnteln (20 = 2,0)
    if strategy == "macd":
        return {"param_a": 12, "param_b": 26, "param_c": 9}
    if strategy == "donchian":
        return {"param_a": 20, "param_b": 10, "param_c": None}
    if strategy == "momentum":
        return {"param_a": 30, "param_b": 0, "param_c": None}
    return {"param_a": 20, "param_b": 50, "param_c": None}  # SMA und der SMA-Teil von Kombiniert


def params_from_inputs(strategy: str, a, b, c, extra=None) -> dict:
    """Formularwerte (Parameter 1-3, bei Kombiniert zusaetzlich die RSI-Felder) in Strategie-Parameter."""
    if strategy == "sma_cross":
        return {"fast": a, "slow": b}
    if strategy == "combo":
        return {"fast": a, "slow": b, **combo_extras(extra or {})}
    if strategy == "bollinger":
        return {"period": a, "k": b / 10}
    if strategy == "macd":
        return {"fast": a, "slow": b, "signal": c or 9}
    if strategy == "donchian":
        return {"entry": a, "exit": b}
    if strategy == "momentum":
        return {"lookback": a, "threshold": float(b)}
    return {"period": a, "low": b, "high": c or 70}
