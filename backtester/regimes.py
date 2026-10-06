"""Marktphasen-Analyse: Wie schlaegt sich die Strategie im Aufwaerts-, Seitwaerts- und Abwaertstrend?

Phase je Kerze = Rendite des Kurses ueber die letzten W Kerzen (nur Vergangenheit, kein Blick nach vorn):
> +Schwelle = Aufwaerts, < -Schwelle = Abwaerts, dazwischen Seitwaerts. W = ca. 90 Tage (bei kurzen Zeitraeumen
weniger). Reine Beschreibung der Daten, nichts wird optimiert. Alles aus den Kurven des Laufs berechnet."""
import numpy as np

from .fmt import de

PER_DAY = {"15m": 96, "1h": 24, "4h": 6, "1d": 1}
KEYS = ["up", "side", "down"]
LABELS = {"up": "Aufwärts", "side": "Seitwärts", "down": "Abwärts"}


def _label(close: np.ndarray, w: int, thr: float) -> np.ndarray:
    lab = np.full(len(close), -1)
    r = close[w:] / close[:-w] - 1
    lab[w:] = np.where(r > thr, 0, np.where(r < -thr, 2, 1))
    return lab


def _segments(lab: np.ndarray, index: list, lo: int, min_len: int) -> list:
    """Zusammenhaengende Phasen fuer die Hinterlegung im Chart; sehr kurze werden der Vorphase zugeschlagen."""
    runs = []
    for i in range(lo, len(lab)):
        if lab[i] < 0:
            continue
        if runs and runs[-1][2] == lab[i] and runs[-1][1] == i - 1:
            runs[-1][1] = i
        else:
            runs.append([i, i, int(lab[i])])
    merged = []
    for a, b, k in runs:
        if merged and (b - a + 1 < min_len or merged[-1][2] == k):
            merged[-1][1] = b
        else:
            merged.append([a, b, k])
    return [{"x0": index[a], "x1": index[b], "key": KEYS[k]} for a, b, k in merged]


def analyze(curves: dict, timeframe: str, kind: str = "single", split_at: str = None) -> dict:
    index = curves.get("index") or []
    close = np.array([c if c is not None else np.nan for c in curves.get("close") or []], dtype=float)
    strat = np.array(curves.get("strategy") or [], dtype=float)
    n = len(close)
    if n < 60 or len(strat) != n or np.isnan(close).any():
        return {"ok": False, "reason": "zu wenige Daten für eine Marktphasen-Analyse"}
    ppd = PER_DAY.get(timeframe, 1)
    w = int(min(90 * ppd, max(20, n // 4)))
    thr = 0.10 * np.sqrt(min(1.0, w / (90 * ppd)))
    lab = _label(close, w, thr)
    lo = max(index.index(split_at), 1) if kind == "split" and split_at in index else 1   # Split: nur Testphase
    sr = np.r_[0.0, strat[1:] / strat[:-1] - 1]
    cr = np.r_[0.0, close[1:] / close[:-1] - 1]
    pos = {ts: i for i, ts in enumerate(index)}
    inpos = np.zeros(n, dtype=bool)
    entries = []
    for t in curves.get("trades") or []:
        i0 = pos.get(t["entry_ts"])
        if i0 is None:
            continue
        i1 = pos.get(t["exit_ts"]) if t.get("exit_ts") else n - 1
        inpos[i0 + 1:(i1 if i1 is not None else n - 1) + 1] = True
        entries.append((i0, t["ret_pct"]))
    idx = np.arange(n)
    ok_range = (idx >= lo) & (lab >= 0)
    total = int(ok_range.sum())
    if total < 30:
        return {"ok": False, "reason": "zu wenige klassifizierte Kerzen (Zeitraum verlängern)"}
    rows = []
    for k, key in enumerate(KEYS):
        m = ok_range & (lab == k)
        cnt = int(m.sum())
        ents = [r for i, r in entries if i >= lo and i < n and lab[i] == k]
        rows.append({
            "key": key, "label": LABELS[key], "candles": cnt, "share_pct": round(cnt / total * 100, 1),
            "strategy_pct": round(float((np.prod(1 + sr[m]) - 1) * 100), 2) if cnt else None,
            "buyhold_pct": round(float((np.prod(1 + cr[m]) - 1) * 100), 2) if cnt else None,
            "time_in_market_pct": round(float(inpos[m].mean() * 100), 1) if cnt else None,
            "trades": len(ents),
            "win_rate_pct": round(sum(r > 0 for r in ents) / len(ents) * 100, 1) if ents else None,
        })
    return {"ok": True, "window": w, "window_days": round(w / ppd, 1), "threshold_pct": round(thr * 100, 1),
            "rows": rows, "segments": _segments(lab, index, lo, max(3, w // 6)), "hint": _hint(rows)}


def _hint(rows: list) -> str:
    big = [r for r in rows if r["share_pct"] >= 10 and r["strategy_pct"] is not None]
    if len(big) < 2:
        return "Die Daten bestehen fast nur aus einer Marktphase, Aussagen über andere Phasen sind nicht möglich."
    best, worst = max(big, key=lambda r: r["strategy_pct"]), min(big, key=lambda r: r["strategy_pct"])
    beats = sum(r["strategy_pct"] > r["buyhold_pct"] for r in big)
    txt = (f"Am besten läuft die Strategie in {best['label']}-Phasen ({de(best['strategy_pct'])} % bei "
           f"{de(best['share_pct'], 1)} % der Zeit), am schlechtesten in {worst['label']}-Phasen "
           f"({de(worst['strategy_pct'])} %). Sie schlägt den Markt in {beats} von {len(big)} Phasen.")
    if worst["strategy_pct"] < 0 and best["strategy_pct"] > 0:
        txt += " Das Ergebnis hängt also stark von der Marktphase ab."
    return txt
