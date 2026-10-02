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


LABELS = {"sma_cross": "SMA-Crossover", "rsi": "RSI"}

STRATEGIES = {
    "sma_cross": (sma_cross, {"fast": 20, "slow": 50}),
    "rsi": (rsi_reversion, {"period": 14, "low": 30, "high": 70}),
}


# Suchraeume fuer die Grid-Search: x-/y-Achse + Funktion, die daraus Parameter baut
GRIDS = {
    "sma_cross": {
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


def params_from_inputs(strategy: str, a, b, c) -> dict:
    """Formularwerte (Parameter 1-3) in Strategie-Parameter uebersetzen."""
    if strategy == "sma_cross":
        return {"fast": a, "slow": b}
    return {"period": a, "low": b, "high": c or 70}
