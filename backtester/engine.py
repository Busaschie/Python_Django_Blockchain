"""Vektorisierte Backtest-Engine (long-only).

Ausfuehrungsmodi (Signal wird am Schluss der Kerze s berechnet):
- "open":  Umsetzung zum Eroeffnungskurs der Folgekerze s+1 (realistischer, Standard)
- "close": Umsetzung zum Schlusskurs der Signalkerze s (optimistischer)
In beiden Faellen gibt es keinen Look-ahead.
Ohne Risikomanagement rechnet die schnelle vektorisierte Variante (`simulate`). Mit Stop-Loss,
Take-Profit, Trailing-Stop oder Positionsgroesse laeuft eine Kerze-fuer-Kerze-Simulation
(`_simulate_risk`), die ohne diese Optionen exakt dieselben Zahlen liefert (siehe tests.py). `fee` steht fuer die gesamten Kosten je Seite
(Gebuehr + Slippage/Spread, werden vor dem Aufruf addiert); sie fallen bei jedem Positionswechsel an.
"""
from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd

VOL_LOOKBACK = 20  # Kerzen fuer die Volatilitaetsschaetzung der Positionsgroesse


def simulate(df: pd.DataFrame, signal: pd.Series, fee: float = 0.001, execution: str = "close"):
    """Liefert Zielposition, Kursrenditen und Strategierenditen je Kerze."""
    a = signal.shift(1).fillna(0)  # Zielposition, ab Kerze t gueltig
    ret = df["close"].pct_change().fillna(0)
    if execution == "open":
        b = a.shift(1).fillna(0)  # Position, die ueber die Kerzengrenze (Vortagesschluss -> Open) gehalten wird
        gap = (df["open"] / df["close"].shift(1) - 1).fillna(0)
        intra = df["close"] / df["open"] - 1
        # Luecke (Vortagesschluss -> Open) und Intraday-Teil wirken nacheinander: multiplikativ verknuepfen
        strat = (1 + b * gap) * (1 + a * intra) - 1 - (a - b).abs() * fee
    else:
        turnover = a.diff().abs().fillna(a.abs())
        strat = a * ret - turnover * fee
    return a, ret, strat


def in_market(pos: pd.Series, execution: str = "close") -> pd.Series:
    """Kerzen, in denen eine Position gehalten wird (im Open-Modus inkl. Ausstiegskerze)."""
    mask = pos == 1
    if execution == "open":  # die Ausstiegskerze (Verkauf zum Open) gehoert noch zum Trade
        mask = mask | (pos.shift(1) == 1)
    return mask


def trade_returns(pos: pd.Series, strat: pd.Series, execution: str = "close") -> pd.Series:
    starts = (pos.diff() == 1) | ((pos == 1) & (pos.index == pos.index[0]))
    trade_id = starts.cumsum()
    in_trade = in_market(pos, execution)
    return (1 + strat[in_trade]).groupby(trade_id[in_trade]).prod() - 1


def _cagr_pct(end_ratio: float, n_bars: int, periods_per_year: int) -> float:
    """Jaehrliche Wachstumsrate in % (end_ratio = Endkapital / Startkapital)."""
    years = n_bars / periods_per_year
    if years <= 0:
        return 0.0
    if end_ratio <= 0:
        return -100.0
    return (end_ratio ** (1 / years) - 1) * 100


def summarize(strat: pd.Series, ret: pd.Series, trade_rets: pd.Series,
              capital: float = 10_000, periods_per_year: int = 365, exposure: pd.Series = None,
              invested: pd.Series = None):
    equity = capital * (1 + strat).cumprod()
    buyhold = capital * (1 + ret).cumprod()
    dd = equity / equity.cummax() - 1
    bh_dd = buyhold / buyhold.cummax() - 1
    ann = np.sqrt(periods_per_year)
    std = strat.std()
    sharpe = float(strat.mean() / std * ann) if std > 0 else 0.0
    downside = float(np.sqrt((strat.clip(upper=0) ** 2).mean()))  # Abwaertsabweichung, Zielrendite 0
    sortino = float(strat.mean() / downside * ann) if downside > 0 else 0.0
    cagr = _cagr_pct(equity.iloc[-1] / capital, len(strat), periods_per_year)
    bh_cagr = _cagr_pct(buyhold.iloc[-1] / capital, len(ret), periods_per_year)
    max_dd = dd.min() * 100
    calmar = cagr / abs(max_dd) if max_dd < 0 else None
    wins, losses = trade_rets[trade_rets > 0].sum(), -trade_rets[trade_rets < 0].sum()
    profit_factor = float(wins / losses) if losses > 0 else None  # None: keine Verlust-Trades
    metrics = {
        "total_return_pct": round((equity.iloc[-1] / capital - 1) * 100, 2),
        "buyhold_return_pct": round((buyhold.iloc[-1] / capital - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "sharpe": round(sharpe, 2),
        "trades": int(len(trade_rets)),
        "win_rate_pct": round(float((trade_rets > 0).mean() * 100), 1) if len(trade_rets) else 0.0,
        "cagr_pct": round(cagr, 2),
        "buyhold_cagr_pct": round(bh_cagr, 2),
        "buyhold_max_drawdown_pct": round(bh_dd.min() * 100, 2),
        "sortino": round(sortino, 2),
        "calmar": round(calmar, 2) if calmar is not None else None,
        "profit_factor": round(profit_factor, 2) if profit_factor is not None else None,
        "time_in_market_pct": round(float(exposure.mean() * 100), 1) if exposure is not None else None,
        # Ø Anteil des Kapitals, der investiert war (nur bei Positionsgroesse < 100 % aussagekraeftig)
        "avg_invested_pct": round(float(invested.mean() * 100), 1) if invested is not None else None,
    }
    return metrics, equity, buyhold


def extract_trades(df: pd.DataFrame, signal: pd.Series, fee: float = 0.001, start: int = 0,
                   execution: str = "close") -> list:
    """Einzelne Trades (Kauf-/Verkaufszeitpunkt, Preise, Netto-Rendite) fuer die Chart-Marker.
    `start`: nur Trades mit Signal-Einstieg ab dieser Kerzenposition (z. B. Testphase)."""
    sig = signal.fillna(0).astype(int)
    delta = sig.diff().fillna(sig.iloc[0]).values
    entries = np.flatnonzero(delta == 1)
    exits = np.flatnonzero(delta == -1)
    close, opn, idx, last = df["close"].values, df["open"].values, df.index, len(df) - 1
    shift = 1 if execution == "open" else 0
    px = opn if execution == "open" else close
    trades = []
    for e in entries:
        if e < start or e >= last:  # Signal auf der letzten Kerze wird nie ausgefuehrt
            continue
        later = exits[exits > e]
        x = int(later[0]) if len(later) else None
        if x is not None and x + shift > last:  # Ausstieg waere erst nach dem Datenende
            x = None
        ei = e + shift
        if x is not None:
            xi = x + shift
            exit_px, exit_ts, n_fees = px[xi], idx[xi].isoformat(), 2
        else:
            exit_px, exit_ts, n_fees = close[last], None, 1  # offen: bewertet zum letzten Schlusskurs
        net = (1 - fee) ** n_fees * exit_px / px[ei] - 1
        trades.append({
            "entry_ts": idx[ei].isoformat(), "entry_px": round(float(px[ei]), 6),
            "exit_ts": exit_ts, "exit_px": round(float(exit_px), 6) if x is not None else None,
            "ret_pct": round(float(net) * 100, 2), "open": x is None,
            "reason": "offen" if x is None else "Signal", "size_pct": 100.0, "sig_i": int(e),
        })
    return trades


def make_curves(close: pd.Series, equity: pd.Series, buyhold: pd.Series, trades: list) -> dict:
    return {
        "index": [t.isoformat() for t in equity.index],
        "strategy": equity.round(2).tolist(),
        "buyhold": buyhold.round(2).tolist(),
        "close": close.reindex(equity.index).round(6).tolist(),
        "trades": trades,
    }


# --------------------------------------------------------------------------------------
# Risikomanagement: Stop-Loss, Take-Profit, Trailing-Stop, Positionsgroesse
# --------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Risk:
    """Alle Werte als Anteil (0.05 = 5 %); None = aus.
    size_mode: "full" (100 %), "fixed" (size_value = Anteil des Kapitals),
               "vol" (size_value = Ziel-Volatilitaet p. a.; Groesse = Ziel / realisierte Vola, max. 100 %)."""
    sl: Optional[float] = None
    tp: Optional[float] = None
    trail: Optional[float] = None
    size_mode: str = "full"
    size_value: float = 1.0

    @property
    def active(self) -> bool:
        return bool(self.sl or self.tp or self.trail or self.size_mode in ("fixed", "vol"))

    @classmethod
    def from_inputs(cls, stop_loss=None, take_profit=None, trailing_stop=None,
                    size_mode="full", size_value=None):
        pct = lambda v: v / 100 if v else None  # noqa: E731
        return cls(pct(stop_loss), pct(take_profit), pct(trailing_stop),
                   size_mode or "full", (size_value or 0) / 100)


@dataclass
class Sim:
    """Ergebnis einer Simulation, einheitlich fuer beide Rechenwege."""
    ret: pd.Series                      # Kursrenditen je Kerze (Buy & Hold)
    strat: pd.Series                    # Strategierenditen je Kerze (nach Kosten)
    mask: pd.Series                     # Kerzen mit Position
    tid: pd.Series                      # Trade-ID je Kerze (0 = keiner)
    invested: Optional[pd.Series] = None
    trades: Optional[list] = None       # inkl. "sig_i" (Signalkerze)

    def trade_rets(self, sl=slice(None)) -> pd.Series:
        s, m, t = self.strat.iloc[sl], self.mask.iloc[sl], self.tid.iloc[sl]
        return (1 + s[m]).groupby(t[m]).prod() - 1

    def parts(self, sl=slice(None)):
        inv = self.invested.iloc[sl] if self.invested is not None else None
        return self.strat.iloc[sl], self.ret.iloc[sl], self.trade_rets(sl), self.mask.iloc[sl], inv

    def metrics(self, ppy: int, sl=slice(None), capital: float = 10_000):
        s, r, tr, m, inv = self.parts(sl)
        return summarize(s, r, tr, capital, ppy, exposure=m, invested=inv)

    def trade_list(self, start: int = 0) -> list:
        """Trades fuer Chart/Tabelle; `start`: nur Signale ab dieser Kerzenposition."""
        return [{k: v for k, v in t.items() if k != "sig_i"}
                for t in (self.trades or []) if t["sig_i"] >= start]


def run_sim(df: pd.DataFrame, signal: pd.Series, cost: float = 0.001, execution: str = "close",
            risk: Optional[Risk] = None, ppy: int = 365, with_trades: bool = False) -> Sim:
    if risk is not None and risk.active:
        return _simulate_risk(df, signal, cost, execution, risk, ppy)
    pos, ret, strat = simulate(df, signal, cost, execution)
    starts = (pos.diff() == 1) | ((pos == 1) & (pos.index == pos.index[0]))
    trades = extract_trades(df, signal, cost, execution=execution) if with_trades else None
    return Sim(ret, strat, in_market(pos, execution), starts.cumsum(), None, trades)


def _simulate_risk(df: pd.DataFrame, signal: pd.Series, cost: float, execution: str,
                   risk: Risk, ppy: int) -> Sim:
    """Kerze-fuer-Kerze-Simulation (long-only) mit Stops und Positionsgroesse.

    Annahmen:
    - Stop-Loss wird vom Einstiegskurs gemessen, der Trailing-Stop vom hoechsten Kurs bis zur
      Vorkerze. Gilt beides, greift der hoehere Stop.
    - Liegt die Eroeffnung bereits jenseits eines Stops oder Ziels (Kurslucke), wird zum
      Eroeffnungskurs ausgefuehrt (beim Stop also schlechter als der Stop-Kurs).
    - Koennen Stop und Take-Profit in derselben Kerze erreicht worden sein, gilt der Stop
      (konservativ, da die Reihenfolge innerhalb der Kerze unbekannt ist).
    - Nach einem Stop-Ausstieg gibt es erst nach einem neuen Kaufsignal (Signal war zwischenzeitlich 0)
      wieder einen Einstieg.
    - Positionsgroesse wird bei Einstieg aus Daten bis zur Signalkerze bestimmt und bis zum Ausstieg gehalten.
    - Kosten fallen anteilig zur Positionsgroesse an.
    """
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    idx, n, open_mode = df.index, len(df), execution == "open"
    sig = signal.fillna(0).astype(int).to_numpy()
    raw_ret = df["close"].pct_change()
    ret = raw_ret.fillna(0)

    if risk.size_mode == "vol":
        vol = (raw_ret.rolling(VOL_LOOKBACK).std() * np.sqrt(ppy)).to_numpy()
        with np.errstate(divide="ignore", invalid="ignore"):
            size = np.where(np.isfinite(vol) & (vol > 0), np.clip(risk.size_value / vol, 0.0, 1.0), 1.0)
    elif risk.size_mode == "fixed":
        size = np.full(n, min(risk.size_value, 1.0))
    else:
        size = np.ones(n)

    strat, invested = np.zeros(n), np.zeros(n)
    mask, tid = np.zeros(n, dtype=bool), np.zeros(n, dtype=int)
    trades, cur = [], None
    f = entry = hwm = 0.0
    blocked, k = False, 0

    def finish(px, exit_i, reason):
        net = (1 - cost) ** 2 * px / entry - 1
        trades.append({**cur, "exit_ts": idx[exit_i].isoformat(), "exit_px": round(float(px), 6),
                       "ret_pct": round(float(net) * 100, 2), "open": False, "reason": reason})

    for t in range(1, n):
        pc, want, f_start, gap_leg, in_leg, costs = c[t - 1], sig[t - 1], f, 0.0, 0.0, 0.0
        if open_mode:
            if f > 0:
                gap_leg = f * (o[t] / pc - 1)  # Kurslucke ueber die Kerzengrenze
            px = ref = o[t]
            exit_i = t
        else:
            px = ref = pc
            exit_i = t - 1

        if f > 0 and want == 0:  # Ausstieg per Signal
            costs += f * cost
            finish(px, exit_i, "Signal")
            f = 0.0
        elif f == 0 and want == 1 and not blocked and size[t - 1] > 1e-9:  # Einstieg
            f = float(size[t - 1])
            costs += f * cost
            entry = hwm = px
            k += 1
            cur = {"entry_ts": idx[t if open_mode else t - 1].isoformat(), "entry_px": round(float(px), 6),
                   "size_pct": round(f * 100, 1), "sig_i": t - 1}
        if want == 0:
            blocked = False

        in_bar = f > 0 or (open_mode and f_start > 0)
        invested[t] = max(f, f_start) if open_mode else f

        if f > 0:
            sl_lvl = entry * (1 - risk.sl) if risk.sl else 0.0
            tr_lvl = hwm * (1 - risk.trail) if risk.trail else 0.0
            stop = max(sl_lvl, tr_lvl)
            tpl = entry * (1 + risk.tp) if risk.tp else np.inf
            exit_px = reason = None
            if stop > 0 and o[t] <= stop:
                exit_px = o[t]
            elif o[t] >= tpl:
                exit_px, reason = o[t], "Take-Profit"
            elif stop > 0 and l[t] <= stop:
                exit_px = stop
            elif h[t] >= tpl:
                exit_px, reason = tpl, "Take-Profit"
            if exit_px is not None:
                reason = reason or ("Trailing-Stop" if tr_lvl > sl_lvl else "Stop-Loss")
                in_leg = f * (exit_px / ref - 1)
                costs += f * cost
                finish(exit_px, t, reason)
                f, blocked = 0.0, True
            else:
                in_leg = f * (c[t] / ref - 1)
                hwm = max(hwm, h[t])

        # Luecke und Kerzenverlauf wirken nacheinander (multiplikativ), Kosten werden abgezogen
        strat[t], mask[t], tid[t] = (1 + gap_leg) * (1 + in_leg) - 1 - costs, in_bar, (k if in_bar else 0)

    if f > 0 and not open_mode and sig[n - 1] == 0:  # Ausstiegssignal auf der letzten Kerze (Close-Modus)
        r_net = (1 - cost) ** 2 * c[-1] / entry - 1
        trades.append({**cur, "exit_ts": idx[n - 1].isoformat(), "exit_px": round(float(c[-1]), 6),
                       "ret_pct": round(float(r_net) * 100, 2), "open": False, "reason": "Signal"})
    elif f > 0:  # Position am Datenende noch offen: zum letzten Schlusskurs bewertet
        net = (1 - cost) * c[-1] / entry - 1
        trades.append({**cur, "exit_ts": None, "exit_px": None, "ret_pct": round(float(net) * 100, 2),
                       "open": True, "reason": "offen"})
    as_s = lambda a: pd.Series(a, index=idx)  # noqa: E731
    return Sim(ret, as_s(strat), as_s(mask), as_s(tid), as_s(invested), trades)


def run_backtest(df: pd.DataFrame, signal: pd.Series, fee: float = 0.001,
                 capital: float = 10_000, periods_per_year: int = 365,
                 execution: str = "close", risk: Optional[Risk] = None) -> dict:
    sim = run_sim(df, signal, fee, execution, risk, periods_per_year, with_trades=True)
    metrics, equity, buyhold = sim.metrics(periods_per_year, capital=capital)
    return {"metrics": metrics, "curves": make_curves(df["close"], equity, buyhold, sim.trade_list())}
