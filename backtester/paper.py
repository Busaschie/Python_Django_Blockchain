"""Paper-Trading: ein virtuelles Konto fuehrt das Signal einer Strategie mit Spielgeld aus und fuehrt ein Journal.

Ablauf bei jeder Pruefung (Knopf oder externer Zeitplan, siehe README):
- die letzten abgeschlossenen Kerzen laden, die Strategie mit den Parametern des Laufs anwenden;
- wechselt das Signal gegenueber der Position, wird zum zuletzt bekannten Kurs (Schluss der letzten abgeschlossenen
  Kerze) gekauft bzw. verkauft; Gebuehr + Slippage wie im Backtest, immer das ganze Konto (kein Hebel, kein Short);
- jede Order kommt ins Journal, der Kontowert wird mit dem aktuellen Kurs bewertet.
Abgleich (`reconcile`): dieselbe Strategie wird auf denselben Kerzen ab Start als normaler Backtest gerechnet. Die
Abweichung zum virtuellen Konto zeigt, ob Backtest und "Realitaet" zusammenpassen. Typische Ursachen: andere
Ausfuehrung (Eroeffnungskurs der Folgekerze im Backtest), Signale, die zwischen zwei Pruefungen kamen und gehen."""
import logging
import os
from datetime import timedelta

import pandas as pd
from django.db import transaction
from django.utils import timezone

from .chains import CHAINS
from .data import MIN_CANDLES, PERIODS_PER_YEAR, fetch_ohlcv
from .engine import run_backtest
from .models import PaperAccount, PaperEntry
from .signals import LOOKBACK_DAYS
from .strategies import STRATEGIES

log = logging.getLogger("tradebot.paper")
COOLDOWN = timedelta(seconds=60)
MAX_LOG = 3000
CAPITAL_RANGE = (100.0, 1_000_000.0)


class PaperError(Exception):
    pass


def max_accounts() -> int:
    try:
        return max(1, int(os.environ.get("PAPER_MAX_PER_USER", "3")))
    except ValueError:
        return 3


def recent_df(chain, timeframe, source, exchange, cache=None):
    """Aktuelle abgeschlossene Kerzen (gleiche Datenquelle wie das Signal)."""
    key = (chain, timeframe, exchange, source)
    if cache is not None and key in cache:
        return cache[key]
    end = timezone.now().date()
    df, _ = fetch_ohlcv(CHAINS[chain]["symbol"], timeframe, end - timedelta(days=LOOKBACK_DAYS[timeframe]), end, source, exchange)
    if cache is not None:
        cache[key] = df
    return df


def _signal(acc, df):
    func, _ = STRATEGIES[acc.strategy]
    return func(df, **acc.params).astype(int)


def _log_point(acc, candle, price):
    acc.equity_log = (acc.equity_log + [[candle, round(acc.equity(price), 2), round(price, 6)]])[-MAX_LOG:]


def _trade(acc, side, candle, price):
    c = acc.cost
    if side == "buy":
        spent = acc.cash
        acc.units = spent * (1 - c) / price
        acc.cash, acc.entry_price = 0.0, price
        paid, ret = spent * c, None
    else:
        gross = acc.units * price
        paid = gross * c
        ret = ((1 - c) ** 2 * price / acc.entry_price - 1) * 100 if acc.entry_price else None
        acc.cash, acc.units, acc.entry_price = gross - paid, 0.0, None
    units = acc.units if side == "buy" else (gross / price)
    return PaperEntry(account=acc, candle=candle, side=side, price=price, units=units, cost_paid=paid,
                      equity_after=acc.equity(price), ret_pct=None if ret is None else round(ret, 2))


def check(acc, df=None, cache=None) -> dict:
    """Eine Pruefung durchfuehren. Gibt {"new": Anzahl neuer Orders, "candle": ..., "skipped": Grund} zurueck."""
    if df is None:
        df = recent_df(acc.chain, acc.timeframe, acc.source, acc.exchange, cache)
    if len(df) < MIN_CANDLES:
        raise PaperError("zu wenige aktuelle Kursdaten")
    sig = _signal(acc, df)
    candle, price = df.index[-1].isoformat(), float(df["close"].iloc[-1])
    desired, new = int(sig.iloc[-1]), 0
    with transaction.atomic():
        if not acc.start_candle:
            acc.start_candle = candle
        if candle != acc.last_candle:
            if desired != acc.position:
                entry = _trade(acc, "buy" if desired else "sell", candle, price)
                entry.save()
                new = 1
            _log_point(acc, candle, price)
            acc.last_candle = candle
        acc.last_price, acc.last_checked, acc.error = price, timezone.now(), ""
        acc.save()
    return {"new": new, "candle": candle, "price": price}


def start(run, user, capital=10_000.0) -> PaperAccount:
    """Konto aus einem fertigen Lauf anlegen und sofort einmal pruefen."""
    if run.status != "done" or run.owner_id != user.pk:
        raise PaperError("Lauf nicht verfügbar")
    if run.source == "synthetic":
        raise PaperError("Bei synthetischen Daten ist kein Paper-Trading möglich (es gibt keine aktuellen Kurse).")
    if not run.params or run.strategy not in STRATEGIES:
        raise PaperError("Der Lauf hat keine Strategie-Parameter.")
    if not CAPITAL_RANGE[0] <= capital <= CAPITAL_RANGE[1]:
        raise PaperError(f"Startkapital zwischen {CAPITAL_RANGE[0]:.0f} und {CAPITAL_RANGE[1]:.0f}.")
    if PaperAccount.objects.filter(owner=user, active=True).count() >= max_accounts():
        raise PaperError(f"Höchstens {max_accounts()} aktive Paper-Konten. Beende zuerst eines.")
    acc = PaperAccount.objects.create(
        owner=user, run=run, name=f"{run.strategy_label} {run.symbol} {run.timeframe}", chain=run.chain, symbol=run.symbol,
        timeframe=run.timeframe, source=run.source, exchange=run.exchange, strategy=run.strategy, params=dict(run.params),
        cost=run.fee + (run.slippage or 0.0), execution=run.execution, start_capital=capital, cash=capital)
    try:
        check(acc)
    except Exception:
        acc.delete()
        raise
    return acc


def refresh(acc) -> dict:
    """Pruefung mit kurzer Sperre gegen Dauer-Klicks."""
    if acc.last_checked and timezone.now() - acc.last_checked < COOLDOWN:
        return {"new": 0, "skipped": "cooldown"}
    return check(acc)


def reconcile(acc, df=None) -> dict:
    """Vergleich virtuelles Konto <-> Backtest derselben Strategie auf denselben Kerzen ab Start."""
    if df is None:
        df = recent_df(acc.chain, acc.timeframe, acc.source, acc.exchange)
    if not acc.start_candle or not acc.last_price:
        return {"ok": False, "reason": "noch keine Prüfung"}
    sig = _signal(acc, df)
    start = df.index.searchsorted(pd.Timestamp(acc.start_candle))
    sl_df, sl_sig = df.iloc[start:], sig.iloc[start:]
    if len(sl_df) < 3:
        return {"ok": False, "reason": "zu wenige Kerzen seit dem Start (mindestens 3 nötig)"}
    bt = run_backtest(sl_df, sl_sig, acc.cost, capital=acc.start_capital,
                      periods_per_year=PERIODS_PER_YEAR[acc.timeframe], execution=acc.execution)["metrics"]
    paper = acc.return_pct
    ref = float(bt["total_return_pct"])
    diff = round(paper - ref, 2)
    n_paper = acc.entries.filter(side="sell").count() + (1 if acc.position else 0)
    n_bt = int(bt.get("trades") or 0)
    gap = abs(diff)
    if gap <= 1.0:
        level, text = "ok", "Backtest und Paper-Konto passen zusammen."
    elif gap <= 3.0:
        level, text = "warn", "Leichte Abweichung zwischen Backtest und Paper-Konto."
    else:
        level, text = "bad", "Deutliche Abweichung zwischen Backtest und Paper-Konto."
    reasons = []
    if acc.execution == "open":
        reasons.append("Der Backtest füllt zum Eröffnungskurs der Folgekerze, das Paper-Konto zum letzten bekannten Schlusskurs.")
    if n_paper != n_bt:
        reasons.append(f"Unterschiedliche Trade-Zahl ({n_paper} im Paper-Konto, {n_bt} im Backtest): Signale zwischen zwei Prüfungen werden nicht ausgeführt.")
    if gap > 1.0 and not reasons:
        reasons.append("Kleine Kursunterschiede beim Füllzeitpunkt summieren sich über die Trades.")
    return {"ok": True, "paper_pct": paper, "backtest_pct": round(ref, 2), "diff_pct": diff,
            "buyhold_pct": round(float(bt.get("buyhold_return_pct", 0.0)), 2), "trades_paper": n_paper, "trades_backtest": n_bt,
            "candles": len(sl_df), "level": level, "verdict": text, "reasons": reasons}


def check_all(cache=None) -> dict:
    """Alle aktiven Konten pruefen (externer Zeitplan). Fehler einzelner Konten stoppen nichts."""
    cache = {} if cache is None else cache
    checked = orders = errors = 0
    for acc in PaperAccount.objects.filter(active=True).select_related("owner"):
        try:
            res = check(acc, cache=cache)
            checked += 1
            orders += res["new"]
        except Exception as exc:  # noqa: BLE001
            errors += 1
            PaperAccount.objects.filter(pk=acc.pk).update(error=str(exc)[:200])
            log.exception("Paper-Prüfung %s fehlgeschlagen", acc.pk)
    return {"checked": checked, "orders": orders, "errors": errors}
