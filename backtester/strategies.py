"""Strategien liefern eine Positionsreihe: 1 = long, 0 = flat (kein Shorting)."""
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


LABELS = {"sma_cross": "SMA-Crossover", "rsi": "RSI", "combo": "Kombiniert (SMA + RSI)"}

STRATEGIES = {
    "sma_cross": (sma_cross, {"fast": 20, "slow": 50}),
    "rsi": (rsi_reversion, {"period": 14, "low": 30, "high": 70}),
    "combo": (combo, {"fast": 20, "slow": 50, "period": 14, "entry": 40, "exit": 70, "logic": "trend"}),
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
    return {"param_a": 20, "param_b": 50, "param_c": None}  # SMA und der SMA-Teil von Kombiniert


def params_from_inputs(strategy: str, a, b, c, extra=None) -> dict:
    """Formularwerte (Parameter 1-3, bei Kombiniert zusaetzlich die RSI-Felder) in Strategie-Parameter."""
    if strategy == "sma_cross":
        return {"fast": a, "slow": b}
    if strategy == "combo":
        return {"fast": a, "slow": b, **combo_extras(extra or {})}
    return {"period": a, "low": b, "high": c or 70}
