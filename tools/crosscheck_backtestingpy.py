#!/usr/bin/env python
"""Abgleich der Backtest-Engine mit der unabhängigen Bibliothek backtesting.py.

Dieselben Kerzen und dieselben Signale laufen durch beide Rechenwege. Verglichen werden
Gesamtrendite, maximaler Drawdown, Zahl der Trades sowie Zeitpunkt und Preis jedes Ein- und Ausstiegs.
Nicht verglichen werden Sharpe (backtesting.py rechnet mit Handelstagen) und Stops/Positionsgrößen.

Start (im Projektordner):
    pip install backtesting
    python tools/crosscheck_backtestingpy.py                      # künstliche Kurse
    python tools/crosscheck_backtestingpy.py --ccxt kraken --symbol BTC/USD --days 730   # echte Kurse
    python tools/crosscheck_backtestingpy.py --csv kurse.csv      # Spalten: timestamp,open,high,low,close,volume
"""
import argparse
import itertools
import os
import sys
import warnings
from datetime import date, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from backtester import indicators, strategies as st  # noqa: E402
from backtester.data import PERIODS_PER_YEAR, synthetic_ohlcv  # noqa: E402
from backtester.engine import run_sim  # noqa: E402

TOL_RETURN_PP = 0.1   # erlaubte Abweichung der Gesamtrendite in Prozentpunkten
TOL_DD_PP = 0.1       # ... des maximalen Drawdowns


def load_ccxt(exchange: str, symbol: str, timeframe: str, days: int) -> pd.DataFrame:
    import ccxt
    ex = getattr(ccxt, exchange)({"enableRateLimit": True})
    tf_ms = {"1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}[timeframe]
    since, rows, now = ex.milliseconds() - days * 86_400_000, [], ex.milliseconds()
    while since < now:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=500)
        new = [r for r in batch if not rows or r[0] > rows[-1][0]]
        if not new:
            break
        rows += new
        since = rows[-1][0] + tf_ms
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df.index = pd.to_datetime(df.pop("ts"), unit="ms", utc=True)
    return df.iloc[:-1]  # laufende Kerze weglassen


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.index = pd.to_datetime(df.pop(df.columns[0]), utc=True)
    return df[["open", "high", "low", "close", "volume"]].astype(float)


def theirs(df: pd.DataFrame, signal: pd.Series, cost: float, execution: str):
    """Dieselben Signale in backtesting.py: Kauf bei 1, Verkauf bei 0, Ausführung wie in unserer Engine."""
    from backtesting import Backtest, Strategy
    sig = signal.to_numpy(dtype=float)

    class FromSignal(Strategy):
        def init(self):
            self.s = self.I(lambda: sig, plot=False)

        def next(self):
            if self.s[-1] == 1 and not self.position:
                self.buy(size=0.99999)  # (fast) voll investiert; viel Kapital, damit ganze Einheiten nicht ins Gewicht fallen
            elif self.s[-1] == 0 and self.position:
                self.position.close()

    data = df.rename(columns=str.capitalize).copy()
    data.index = data.index.tz_convert("UTC").tz_localize(None)
    bt = Backtest(data, FromSignal, cash=1e10, commission=cost, trade_on_close=(execution == "close"),
                  exclusive_orders=True, finalize_trades=False)
    stats = bt.run()
    t = stats["_trades"]
    return {"return": float(stats["Return [%]"]), "dd": float(stats["Max. Drawdown [%]"]),
            "trades": [(pd.Timestamp(r.EntryTime), float(r.EntryPrice), pd.Timestamp(r.ExitTime), float(r.ExitPrice))
                       for r in t.itertuples()]}


def ours(df: pd.DataFrame, signal: pd.Series, cost: float, execution: str, ppy: int):
    sim = run_sim(df, signal, cost, execution, None, ppy, with_trades=True)
    m = sim.metrics(ppy)[0]
    closed = [t for t in sim.trade_list() if not t.get("open")]
    tz = lambda x: pd.Timestamp(x).tz_convert("UTC").tz_localize(None)  # noqa: E731
    return {"return": m["total_return_pct"], "dd": m["max_drawdown_pct"],
            "trades": [(tz(t["entry_ts"]), t["entry_px"], tz(t["exit_ts"]), t["exit_px"]) for t in closed],
            "n_all": len(sim.trade_list())}


def compare_trades(a, b):
    """(gleich viele Trades, gleiche Zeitpunkte, größte relative Preisabweichung)"""
    if len(a) != len(b):
        return False, False, float("nan")
    same_time = all(x[0] == y[0] and x[2] == y[2] for x, y in zip(a, b))
    dev = max((max(abs(x[1] / y[1] - 1), abs(x[3] / y[3] - 1)) for x, y in zip(a, b)), default=0.0)
    return True, same_time, dev


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ccxt", help="Börse für echte Kurse, z. B. kraken")
    ap.add_argument("--symbol", default="BTC/USD")
    ap.add_argument("--csv", help="CSV-Datei mit Kursen")
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--timeframe", default="1d", choices=["1h", "4h", "1d"])
    args = ap.parse_args()
    warnings.filterwarnings("ignore")

    if args.ccxt:
        datasets = {f"{args.ccxt} {args.symbol} {args.timeframe}": load_ccxt(args.ccxt, args.symbol, args.timeframe, args.days)}
    elif args.csv:
        datasets = {args.csv: load_csv(args.csv)}
    else:
        end = date.today() - timedelta(days=1)
        datasets = {f"synthetisch {s} {tf}": synthetic_ohlcv(s, tf, end - timedelta(days=days), end)
                    for s, tf, days in [("BTC/USDT", "1d", 900), ("ETH/USDT", "1d", 900),
                                        ("SOL/USDT", "1d", 900), ("BTC/USDT", "1h", 120)]}

    strategies = {"SMA 20/50": ("sma_cross", {"fast": 20, "slow": 50}),
                  "SMA 10/40": ("sma_cross", {"fast": 10, "slow": 40}),
                  "RSI 14/30/70": ("rsi", {"period": 14, "low": 30, "high": 70}),
                  "RSI 7/25/75": ("rsi", {"period": 7, "low": 25, "high": 75}),
                  "Kombi trend": ("combo", {"logic": "trend"}), "Kombi oder": ("combo", {"logic": "or"})}
    print(f"Indikatoren: {indicators.backend()}\n")
    header = f"{'Daten':26} {'Strategie':13} {'Ausf.':5} {'Kosten':7} {'Trades':>6} {'Rendite uns':>11} {'backtesting.py':>14} {'Δ pp':>7} {'Δ DD pp':>8} {'Zeiten':>6} {'Preis Δ':>8}"
    print(header + "\n" + "-" * len(header))
    bad = total = 0
    max_ret = max_dd = max_px = 0.0
    for (dname, df), (sname, (key, params)), execution, cost in itertools.product(
            datasets.items(), strategies.items(), ("open", "close"), (0.0, 0.0015)):
        ppy = PERIODS_PER_YEAR["1h" if dname.endswith("1h") else "1d"]
        signal = st.STRATEGIES[key][0](df, **params)
        o, t = ours(df, signal, cost, execution, ppy), theirs(df, signal, cost, execution)
        same_n, same_t, dev = compare_trades(o["trades"], t["trades"])
        d_ret, d_dd = o["return"] - t["return"], o["dd"] - t["dd"]
        ok = same_n and same_t and abs(d_ret) <= TOL_RETURN_PP and abs(d_dd) <= TOL_DD_PP
        total += 1
        max_ret, max_dd = max(max_ret, abs(d_ret)), max(max_dd, abs(d_dd))
        max_px = max(max_px, dev if dev == dev else 0.0)
        bad += not ok
        print(f"{dname:26} {sname:13} {execution:5} {cost:<7} {len(o['trades']):>2}/{len(t['trades']):<3} "
              f"{o['return']:>10.2f}% {t['return']:>13.2f}% {d_ret:>7.3f} {d_dd:>8.3f} {'ja' if same_t else 'NEIN':>6} "
              f"{dev * 100:>7.4f}%" + ("" if ok else "   <-- ABWEICHUNG"))
    print(f"\n{total - bad} von {total} Vergleichen innerhalb der Toleranz "
          f"(Rendite und Drawdown ±{TOL_RETURN_PP} Prozentpunkte, gleiche Trades und Zeitpunkte).")
    print(f"Größte Abweichung: Rendite {max_ret:.3f} Prozentpunkte, Drawdown {max_dd:.3f} Prozentpunkte, "
          f"Ein-/Ausstiegspreis {max_px * 100:.4f} %.")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
