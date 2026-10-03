"""Indikatoren: TA-Lib, falls installiert, sonst pandas-Fallback."""
import pandas as pd

try:
    import talib
except ImportError:  # TA-Lib braucht eine C-Bibliothek
    talib = None


def backend() -> str:
    """Welche Bibliothek tatsaechlich rechnet (wird beim Lauf gespeichert und angezeigt)."""
    return "TA-Lib" if talib else "pandas (Ersatz)"


def sma(close: pd.Series, n: int) -> pd.Series:
    if talib:
        return pd.Series(talib.SMA(close.values, n), index=close.index)
    return close.rolling(n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    if talib:
        return pd.Series(talib.RSI(close.values, n), index=close.index)
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)
