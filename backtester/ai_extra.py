"""KI-Erweiterungen: Laufvergleich, naechste Parametervariante, feste Fragen zum Ergebnis.

Gemeinsame Regeln (wie beim KI-Kommentar):
- Alle Zahlen und alle Schlussfolgerungen entstehen zuerst regelbasiert aus bereits berechneten Ergebnissen. Die KI
  formuliert das nur um; jede Zahl ihrer Antwort wird gegen das Datenblatt geprueft (`ai.unknown_numbers`), sonst gilt
  der regelbasierte Text. Ohne KI-Schluessel oder bei erreichtem Tageslimit gibt es ebenfalls den regelbasierten Text.
- Es gibt kein Freitext-Eingabefeld: Fragen kommen aus einer festen Liste, Zeitraeume aus den Daten des Laufs.
- Antworten werden je Lauf (und Frage) gespeichert; eine wiederholte Frage kostet nichts."""
import json
import logging
import os
from datetime import datetime

import numpy as np

from . import ai, stability
from .fmt import de
from .models import AiCall, AiResult
from .strategies import inputs_from_params

log = logging.getLogger("tradebot.ai")

BASE_RULES = """Du bist ein sachlicher Assistent in einer Backtest-Software für Kryptowährungs-Strategien.
Du formulierst vorgegebene Ergebnisse für Laien auf Deutsch um.
Regeln:
- Verwende ausschließlich Zahlen aus dem Datenblatt. Rechne nichts selbst, schätze nichts, runde nicht um.
- Behaupte keine Ursachen, die nicht im Datenblatt unter "gruende" oder "erkenntnisse" stehen.
- Keine Kauf- oder Verkaufsempfehlung, keine Prognose. Alles bezieht sich auf vergangene Daten.
- Kurze, klare Sätze. Keine Markdown-Formatierung."""


# ---------------------------------------------------------------- gemeinsamer KI-Ablauf
def _clean(s, n=400):
    import re
    return re.sub(r"[*_`#]+", "", str(s)).strip()[:n]


def llm_rewrite(user, system: str, facts: dict, parse, text_of):
    """(Antwort, Quelle) oder (None, Hinweis). `parse(json_text)` -> dict oder ValueError; `text_of(dict)` -> Text fuer die Zahlenpruefung."""
    if not os.environ.get("GROQ_API_KEY"):
        return None, "KI nicht eingerichtet, regelbasierte Antwort."
    if ai.remaining_today(user) <= 0:
        return None, f"Tageslimit für KI-Antworten erreicht ({ai.limits()[0]} je Benutzer), regelbasierte Antwort."
    text = ai.facts_text(facts)
    call = AiCall.objects.create(user=user)
    models = [os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"), os.environ.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")]
    last = ""
    for model in dict.fromkeys(models):
        messages = [{"role": "system", "content": system}, {"role": "user", "content": "Datenblatt:\n" + text}]
        for _ in range(2):
            try:
                answer = ai.chat(model, messages)
                data = parse(answer)
            except (ai.AiError, ValueError, KeyError) as exc:
                last = f"{model}: {exc}"
                break
            bad = ai.unknown_numbers(text_of(data), text)
            if not bad:
                call.ok, call.model = True, model
                call.save()
                return data, f"groq:{model}"
            last = f"{model}: Zahlen nicht im Datenblatt: {bad[:5]}"
            messages += [{"role": "assistant", "content": answer},
                         {"role": "user", "content": "Diese Zahlen stehen nicht im Datenblatt: " + ", ".join(str(b) for b in bad[:6])
                          + ". Verwende nur Zahlen aus dem Datenblatt. Antworte erneut als JSON."}]
    call.save()
    log.warning("KI-Antwort fehlgeschlagen: %s", last)
    return None, "KI momentan nicht verfügbar oder Antwort nicht verwertbar, regelbasierte Antwort."


def _json(text):
    import re
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            raise ValueError("keine JSON-Antwort") from None
        return json.loads(m.group(0))


def _store(user, kind, run, run2=None, key="", payload=None, source="regeln"):
    obj, _ = AiResult.objects.update_or_create(owner=user, kind=kind, run=run, run2=run2, key=key,
                                               defaults={"payload": payload or {}, "source": source})
    return obj


def _get(user, kind, run, run2=None, key=""):
    return AiResult.objects.filter(owner=user, kind=kind, run=run, run2=run2, key=key).first()


# ================================================================ 1) Laufvergleich
CMP_METRICS = (("total_return_pct", "Rendite", True), ("max_drawdown_pct", "größter Rückgang", True),
               ("sharpe", "Sharpe", True), ("win_rate_pct", "Trefferquote", True), ("trades", "Trades", None))


def _setting_view(run) -> dict:
    return {"strategie": run.strategy_label, "parameter": run.params, "chain": run.symbol, "zeitfenster": run.timeframe,
            "modus": run.mode_label, "zeitraum": run.period_label, "kosten_je_seite_pct": round((run.fee + (run.slippage or 0)) * 100, 4),
            "risiko": run.risk_label or "keines", "ausfuehrung": run.execution_label}


def compare_facts(a, b) -> dict:
    ma, mb = a.metrics or {}, b.metrics or {}
    sa, sb = _setting_view(a), _setting_view(b)
    diff_settings = [k for k in sa if sa[k] != sb[k]]
    rows = {}
    better = {}
    for key, name, higher_better in CMP_METRICS:
        va, vb = ma.get(key), mb.get(key)
        if va is None or vb is None:
            continue
        rows[name] = {"lauf_a": va, "lauf_b": vb, "differenz": round(vb - va, 2)}
        if higher_better and va != vb:
            # Drawdown ist negativ: weniger negativ = besser, also gilt "hoeher ist besser" auch hier
            better[name] = "A" if va > vb else "B"
    comparable = not ({"chain", "zeitfenster", "zeitraum"} & set(diff_settings))
    f = {"lauf_a": {"einstellungen": sa, "kennzahlen": {k: ma.get(k) for k, _, _ in CMP_METRICS},
                    "buyhold_return_pct": ma.get("buyhold_return_pct"), "plausibilitaet": ((a.curves or {}).get("plaus") or {}).get("level")},
         "lauf_b": {"einstellungen": sb, "kennzahlen": {k: mb.get(k) for k, _, _ in CMP_METRICS},
                    "buyhold_return_pct": mb.get("buyhold_return_pct"), "plausibilitaet": ((b.curves or {}).get("plaus") or {}).get("level")},
         "unterschiedliche_einstellungen": diff_settings, "kennzahlen_vergleich": rows, "besser_in": better,
         "direkt_vergleichbar": comparable}
    return f


def compare_rules(f: dict) -> dict:
    rows, better, diff = f["kennzahlen_vergleich"], f["besser_in"], f["unterschiedliche_einstellungen"]
    parts = []
    for name in ("Rendite", "größter Rückgang", "Sharpe"):
        if name in rows:
            r = rows[name]
            who = better.get(name)
            parts.append(f"{name}: A {de(r['lauf_a'])}, B {de(r['lauf_b'])}" + (f" (Vorteil {who})" if who else ""))
    summary = "; ".join(parts) + "." if parts else "Zu wenig Kennzahlen für einen Vergleich."
    items = []
    if diff:
        items.append("Unterschiedlich eingestellt: " + ", ".join(diff) + ".")
    else:
        items.append("Beide Läufe haben dieselben Einstellungen.")
    if "Trades" in rows:
        r = rows["Trades"]
        items.append(f"Trades: A {int(r['lauf_a'])}, B {int(r['lauf_b'])}.")
    for k in ("A", "B"):
        pl = f["lauf_" + k.lower()].get("plausibilitaet")
        if pl in ("warn", "bad"):
            items.append(f"Lauf {k} hat Hinweise in der Plausibilitäts-Ampel, sein Ergebnis ist weniger belastbar.")
    verdict = ("Die Läufe sind direkt vergleichbar." if f["direkt_vergleichbar"]
               else "Chain, Zeitfenster oder Zeitraum unterscheiden sich: die Kennzahlen sind nur eingeschränkt vergleichbar.")
    wins = list(f["besser_in"].values())
    if wins and f["direkt_vergleichbar"]:
        a, b = wins.count("A"), wins.count("B")
        verdict += f" Lauf A ist in {a}, Lauf B in {b} von {len(wins)} Kennzahlen besser."
    return {"zusammenfassung": summary, "unterschiede": items[:4], "fazit": verdict}


def _parse_compare(text):
    d = _json(text)
    out = {"zusammenfassung": _clean(d.get("zusammenfassung", ""), 700), "fazit": _clean(d.get("fazit", ""), 400)}
    items = d.get("unterschiede") or []
    out["unterschiede"] = [_clean(i, 300) for i in ([items] if isinstance(items, str) else items) if str(i).strip()][:4]
    if not out["zusammenfassung"] or not out["fazit"]:
        raise ValueError("Antwort unvollständig")
    return out


def compare_runs(a, b, user) -> AiResult:
    if a.pk == b.pk:
        raise ValueError("Bitte zwei verschiedene Läufe wählen.")
    existing = _get(user, "compare", a, b)
    if existing:
        return existing
    f = compare_facts(a, b)
    rules = compare_rules(f)
    system = BASE_RULES + """
Aufgabe: Erkläre den Unterschied zwischen Lauf A und Lauf B. Sage, wo welcher Lauf besser ist und woran das laut Datenblatt liegen kann (unterschiedliche Einstellungen). Ist "direkt_vergleichbar" false, weise darauf hin.
Antworte nur als JSON mit den Schlüsseln "zusammenfassung" (Text, max. 90 Wörter), "unterschiede" (Liste, höchstens 4 kurze Sätze), "fazit" (ein Satz)."""
    data, source = llm_rewrite(user, system, f, _parse_compare,
                               lambda d: " ".join([d["zusammenfassung"], *d["unterschiede"], d["fazit"]]))
    payload = {**(data or rules), "facts_ok": True}
    if not data:
        payload["note"] = source
    return _store(user, "compare", a, b, "", payload, source if data else "regeln")


# ================================================================ 2) naechste Parametervariante
def _plateau_score(sh, j, i, min_cells=1):
    """Mittel der Sharpe-Werte im 3x3-Fenster ohne den hoechsten Wert: eine einzelne Spitze zaehlt nicht als Plateau."""
    vals = [sh[y][x] for y in range(max(0, j - 1), min(len(sh), j + 2)) for x in range(max(0, i - 1), min(len(sh[0]), i + 2))
            if sh[y][x] is not None]
    if len(vals) < max(2, min_cells):
        return None
    vals.remove(max(vals))
    return float(np.mean(vals))


def suggest_facts(run):
    """(Datenblatt, Formularwerte) oder (None, Grund)."""
    st = (run.curves or {}).get("stab") or {}
    if not st.get("ok"):
        return None, st.get("reason") or "Für diesen Lauf gibt es keine Parameter-Stabilität (Einzellauf oder Train/Test nötig)."
    xs, ys, ret, shp = st["x"], st["y"], st["ret"], st["sharpe"]
    ax = stability._axes(run.strategy, dict(run.params))
    if ax is None:
        return None, "Für diese Strategie nicht vorgesehen."
    build = ax[4]
    cx, cy = st["center"]["x"], st["center"]["y"]
    ix, iy = xs.index(cx), ys.index(cy)
    center_score = _plateau_score(shp, iy, ix)
    best = None
    for j in range(len(ys)):
        for i in range(len(xs)):
            if (i, j) == (ix, iy) or ret[j][i] is None or shp[j][i] is None or build(xs[i], ys[j]) is None:
                continue
            score = _plateau_score(shp, j, i, min_cells=5)   # nur Punkte mit mindestens 5 gueltigen Nachbarfeldern
            if score is None or ret[j][i] <= 0:
                continue
            if best is None or score > best[0]:
                best = (score, i, j)
    base = {"strategie": run.strategy_label, "x_name": st["x_name"], "y_name": st["y_name"],
            "aktuell": {st["x_name"]: cx, st["y_name"]: cy, "rendite_pct": ret[iy][ix], "sharpe": shp[iy][ix],
                        "plateau_sharpe": None if center_score is None else round(center_score, 2)},
            "stabilitaet": st.get("level"), "grundlage": "Testdaten des Laufs" if st.get("on_test") else "Zeitraum des Laufs"}
    if best is None or center_score is None or best[0] <= center_score + 0.05:
        base["vorschlag"] = None
        return base, None
    score, i, j = best
    params = build(xs[i], ys[j])
    base["vorschlag"] = {st["x_name"]: xs[i], st["y_name"]: ys[j], "rendite_pct": ret[j][i], "sharpe": shp[j][i],
                         "plateau_sharpe": round(score, 2)}
    base["_params"] = params
    return base, None


def suggest_rules(f: dict) -> dict:
    cur, sug = f["aktuell"], f.get("vorschlag")
    xn, yn = f["x_name"], f["y_name"]
    if not sug:
        return {"zusammenfassung": f"Der aktuelle Punkt ({xn} {cur[xn]}, {yn} {cur[yn]}) liegt bereits auf dem besten Plateau des "
                                   f"getesteten Rasters (Sharpe {de(cur['sharpe'])}). Es gibt keine bessere Nachbarvariante.",
                "begruendung": ["Eine andere Variante der Parameter bringt hier keinen belastbaren Vorteil.",
                                "Sinnvoller sind andere Tests: längerer Zeitraum, Walk-Forward oder höhere Kosten."],
                "hinweis": "Nur ein Vorschlag aus vergangenen Daten, keine Empfehlung."}
    return {"zusammenfassung": f"Als nächste Variante bietet sich {xn} {sug[xn]} und {yn} {sug[yn]} an "
                               f"(bisher {xn} {cur[xn]}, {yn} {cur[yn]}).",
            "begruendung": [f"Rendite {de(sug['rendite_pct'])} % gegenüber {de(cur['rendite_pct'])} % beim aktuellen Punkt.",
                            f"Sharpe {de(sug['sharpe'])} gegenüber {de(cur['sharpe'])}; im Mittel mit den Nachbarpunkten "
                            f"{de(sug['plateau_sharpe'])} gegenüber {de(cur['plateau_sharpe'])}.",
                            "Gewählt wurde ein Punkt mit guten Nachbarn (Plateau), nicht die höchste Einzelzahl."],
            "hinweis": f"Die Zahlen stammen aus den {f['grundlage']}. Ob die Variante hält, zeigt erst ein neuer Lauf, am besten mit Walk-Forward."}


def _parse_suggest(text):
    d = _json(text)
    items = d.get("begruendung") or []
    out = {"zusammenfassung": _clean(d.get("zusammenfassung", ""), 500),
           "begruendung": [_clean(i, 300) for i in ([items] if isinstance(items, str) else items) if str(i).strip()][:3],
           "hinweis": _clean(d.get("hinweis", ""), 300)}
    if not out["zusammenfassung"] or not out["hinweis"]:
        raise ValueError("Antwort unvollständig")
    return out


def form_values(run, params) -> dict:
    """Formularwerte fuer 'Variante uebernehmen' (Einzellauf mit diesen Parametern)."""
    return {"strategy": run.strategy, "mode": "single", **inputs_from_params(run.strategy, params)}


def suggest_next(run, user) -> AiResult:
    existing = _get(user, "suggest", run)
    if existing:
        return existing
    f, reason = suggest_facts(run)
    if f is None:
        return _store(user, "suggest", run, payload={"ok": False, "reason": reason})
    params = f.pop("_params", None)
    rules = suggest_rules(f)
    system = BASE_RULES + """
Aufgabe: Erkläre, warum die vorgeschlagene Parametervariante als nächster Test sinnvoll ist (oder warum es keine bessere gibt, wenn "vorschlag" null ist). Erwähne, dass nur ein Test im Programm die Variante bestätigen kann.
Antworte nur als JSON mit den Schlüsseln "zusammenfassung" (Text, max. 60 Wörter), "begruendung" (Liste, höchstens 3 kurze Sätze), "hinweis" (ein Satz)."""
    data, source = llm_rewrite(user, system, f, _parse_suggest,
                               lambda d: " ".join([d["zusammenfassung"], *d["begruendung"], d["hinweis"]]))
    payload = {"ok": True, **(data or rules), "form": form_values(run, params) if params else None, "params": params}
    if not data:
        payload["note"] = source
    return _store(user, "suggest", run, payload=payload, source=source if data else "regeln")


# ================================================================ 3) feste Fragen
QUESTIONS = (
    ("worst", "Welcher Zeitraum war am schwächsten – und warum?"),
    ("best", "Welcher Zeitraum war am stärksten – und warum?"),
    ("period", "Wie lief ein bestimmter Zeitraum – und warum?"),
    ("drawdown", "Wann und warum entstand der größte Rückgang?"),
    ("vs_buyhold", "Warum liegt die Strategie vor bzw. hinter Buy & Hold?"),
    ("reliability", "Wie belastbar ist das Ergebnis?"),
    ("costs", "Wie stark belasten die Kosten das Ergebnis?"),
)
QUESTION_IDS = {q for q, _ in QUESTIONS}


def question_title(qid: str, arg: str = "") -> str:
    t = dict(QUESTIONS).get(qid, qid)
    return t.replace("ein bestimmter Zeitraum", arg) if qid == "period" and arg else t


def _series(run):
    c = run.curves or {}
    idx = c.get("index") or []
    st, bh = np.array(c.get("strategy") or [], dtype=float), np.array(c.get("buyhold") or [], dtype=float)
    if not idx or len(st) != len(idx) or len(bh) != len(idx):
        return None
    return idx, st, bh, c.get("trades") or []


def buckets(run) -> list:
    """Zeitraeume des Laufs: Kalenderjahre (>= 2 Jahre), sonst Quartale (> 120 Tage), sonst Monate. [(Schluessel, i0, i1)]"""
    s = _series(run)
    if not s:
        return []
    idx = s[0]
    days = (datetime.fromisoformat(idx[-1][:19]) - datetime.fromisoformat(idx[0][:19])).days
    years = {t[:4] for t in idx}

    def key(t):
        if len(years) >= 2:
            return t[:4]
        m = int(t[5:7])
        return f"{t[:4]}-Q{(m - 1) // 3 + 1}" if days > 120 else t[:7]
    out, start, cur = [], 0, key(idx[0])
    for i, t in enumerate(idx):
        k = key(t)
        if k != cur:
            out.append((cur, start, i - 1))
            cur, start = k, i
    out.append((cur, start, len(idx) - 1))
    return [b for b in out if b[2] - b[1] >= 4]   # mindestens 5 Kerzen


def _bucket_stats(run, i0, i1) -> dict:
    idx, st, bh, trades = _series(run)
    base = max(i0 - 1, 0)
    pct = lambda a: round(float((a[i1] / a[base] - 1) * 100), 2)  # noqa: E731
    seg = st[i0:i1 + 1]
    dd = float((seg / np.maximum.accumulate(seg) - 1).min() * 100)
    t0, t1 = idx[i0], idx[i1]
    closed = [t for t in trades if t.get("exit_ts") and t0 <= t["exit_ts"] <= t1]
    held = 0
    for t in trades:
        e, x = t["entry_ts"], t.get("exit_ts") or idx[-1]
        a, b = max(e, t0), min(x, t1)
        if a < b:
            held += sum(1 for ts in idx[i0:i1 + 1] if a <= ts < b)
    wins = sum(t["ret_pct"] > 0 for t in closed)
    return {"zeitraum": f"{t0[:10]} bis {t1[:10]}", "kerzen": i1 - i0 + 1, "strategie_pct": pct(st), "buyhold_pct": pct(bh),
            "max_rueckgang_pct": round(dd, 2), "trades": len(closed),
            "trefferquote_pct": round(wins / len(closed) * 100, 1) if closed else None,
            "im_markt_pct": round(held / (i1 - i0 + 1) * 100, 1)}


def _reasons(s: dict, cost_pct: float) -> list:
    out = []
    bh, sp = s["buyhold_pct"], s["strategie_pct"]
    if bh <= -10:
        out.append(f"Der Markt fiel in diesem Zeitraum um {de(abs(bh))} % (Buy & Hold {de(bh)} %).")
    elif bh >= 10:
        out.append(f"Der Markt stieg in diesem Zeitraum um {de(bh)} % (Buy & Hold).")
    if bh >= 10 and s["im_markt_pct"] < 40:
        out.append(f"Die Strategie war nur zu {de(s['im_markt_pct'])} % der Zeit investiert und hat den Anstieg großteils verpasst.")
    if bh <= -10 and s["im_markt_pct"] >= 60 and sp < 0:
        out.append(f"Die Strategie war zu {de(s['im_markt_pct'])} % der Zeit investiert, während der Markt fiel.")
    if bh <= -10 and s["im_markt_pct"] < 40 and sp > bh:
        out.append(f"Sie war nur zu {de(s['im_markt_pct'])} % investiert und hat den Rückgang so teilweise vermieden.")
    if s["trades"] >= 3 and s["trefferquote_pct"] is not None and s["trefferquote_pct"] < 40:
        out.append(f"{s['trades']} Trades bei nur {de(s['trefferquote_pct'])} % Trefferquote: viele kleine Verluste, typisch für Seitwärtsphasen.")
    drag = round(s["trades"] * 2 * cost_pct, 2)
    if s["trades"] >= 4 and drag >= 1.0:
        out.append(f"Die Kosten von {de(cost_pct, 3)} % je Seite summieren sich bei {s['trades']} Trades auf etwa {de(drag)} Prozentpunkte.")
    if not out:
        out.append("Aus den berechneten Zahlen ist keine einzelne Hauptursache erkennbar.")
    return out


def _period_pick(run, qid, arg):
    bs = buckets(run)
    if not bs:
        return None, None
    stats = {k: _bucket_stats(run, i0, i1) for k, i0, i1 in bs}
    if qid == "worst":
        k = min(stats, key=lambda x: stats[x]["strategie_pct"])
    elif qid == "best":
        k = max(stats, key=lambda x: stats[x]["strategie_pct"])
    else:
        if arg not in stats:
            raise ValueError("Unbekannter Zeitraum für diesen Lauf.")
        k = arg
    return k, stats


def ask_facts(run, qid, arg="") -> dict:
    """Datenblatt zur Frage. Wirft ValueError bei unbekannter Frage oder unbekanntem Zeitraum."""
    if qid not in QUESTION_IDS:
        raise ValueError("Unbekannte Frage.")
    m, c = run.metrics or {}, run.curves or {}
    cost_pct = round((run.fee + (run.slippage or 0)) * 100, 4)
    f = {"frage": question_title(qid, arg), "strategie": run.strategy_label, "chain": run.symbol, "zeitraum": run.period_label}
    if qid in ("worst", "best", "period"):
        k, stats = _period_pick(run, qid, arg)
        if k is None:
            raise ValueError("Für diesen Lauf gibt es keine Kurven.")
        s = stats[k]
        f.update({"gewaehlter_zeitraum": k, "kennzahlen_zeitraum": s, "gruende": _reasons(s, cost_pct),
                  "alle_zeitraeume": {kk: {"strategie_pct": v["strategie_pct"], "buyhold_pct": v["buyhold_pct"]} for kk, v in stats.items()}})
    elif qid == "drawdown":
        idx, st, bh, trades = _series(run)
        peak = np.maximum.accumulate(st)
        dd = st / peak - 1
        tr = int(dd.argmin())
        pk = int(np.flatnonzero(st[:tr + 1] == peak[tr])[-1])      # letztes Hoch vor dem Tief
        rec = next((i for i in range(tr, len(st)) if st[i] >= st[pk]), None)
        s = _bucket_stats(run, pk, tr) if tr - pk >= 4 else None
        f.update({"hoch": idx[pk][:10], "tief": idx[tr][:10], "rueckgang_pct": round(float(dd[tr] * 100), 2),
                  "erholt_am": idx[rec][:10] if rec is not None else None, "dauer_kerzen_bis_tief": tr - pk,
                  "buyhold_im_fenster_pct": round(float((bh[tr] / bh[pk] - 1) * 100), 2),
                  "buyhold_max_rueckgang_pct": m.get("buyhold_max_drawdown_pct"), "kennzahlen_fenster": s})
        f["gruende"] = _reasons(s, cost_pct) if s else ["Der Rückgang war sehr kurz; keine weitere Aufschlüsselung."]
        if rec is None:
            f["gruende"].append("Der Rückgang war am Ende des Laufs noch nicht wieder aufgeholt.")
    elif qid == "vs_buyhold":
        bs = buckets(run)
        wins = loss = 0
        for _, i0, i1 in bs:
            s = _bucket_stats(run, i0, i1)
            wins += s["strategie_pct"] > s["buyhold_pct"]
            loss += s["strategie_pct"] <= s["buyhold_pct"]
        f.update({"rendite_pct": m.get("total_return_pct"), "buyhold_pct": m.get("buyhold_return_pct"),
                  "max_rueckgang_pct": m.get("max_drawdown_pct"), "buyhold_max_rueckgang_pct": m.get("buyhold_max_drawdown_pct"),
                  "im_markt_pct": m.get("time_in_market_pct"), "trades": m.get("trades"),
                  "zeitraeume_besser": wins, "zeitraeume_schlechter": loss})
        g = []
        r, b = m.get("total_return_pct"), m.get("buyhold_return_pct")
        if r is not None and b is not None:
            g.append(f"Rendite {de(r)} % gegenüber {de(b)} % bei Buy & Hold.")
        if m.get("time_in_market_pct") is not None and b is not None and b > 0 and m["time_in_market_pct"] < 60:
            g.append(f"Die Strategie war nur zu {de(m['time_in_market_pct'])} % investiert; im steigenden Markt kostet das Rendite.")
        if m.get("max_drawdown_pct") is not None and m.get("buyhold_max_drawdown_pct") is not None \
                and abs(m["max_drawdown_pct"]) < abs(m["buyhold_max_drawdown_pct"]):
            g.append(f"Dafür war der größte Rückgang mit {de(m['max_drawdown_pct'])} % kleiner als bei Buy & Hold ({de(m['buyhold_max_drawdown_pct'])} %).")
        if wins + loss:
            g.append(f"In {wins} von {wins + loss} Zeiträumen lag die Strategie vor Buy & Hold.")
        f["gruende"] = g or ["Zu wenig Daten für eine Aufschlüsselung."]
    elif qid == "reliability":
        pl, mc, ct = c.get("plaus") or {}, c.get("mc") or {}, c.get("cost") or {}
        sab = c.get("stab") or {}
        f.update({"ampel": pl.get("level"), "trades": m.get("trades"),
                  "auffaellig": [f"{x['title']}: {x['detail']}" for x in pl.get("checks", []) if x["level"] != "ok"][:4],
                  "zufallsvergleich_p": mc.get("p_value") if mc.get("ok") else None,
                  "sharpe_intervall": ((mc.get("ci") or {}).get("sharpe")) if mc.get("ok") else None,
                  "stabilitaet": sab.get("level") if sab.get("ok") else None})
        g = []
        n = m.get("trades") or 0
        g.append(f"{n} Trades: " + ("ausreichend für eine erste Aussage." if n >= 30 else "unter 30, statistisch nur ein Anhaltspunkt."))
        if f["ampel"]:
            g.append({"ok": "Die Plausibilitäts-Ampel ist grün.", "warn": "Die Plausibilitäts-Ampel zeigt Hinweise.",
                      "bad": "Die Plausibilitäts-Ampel ist rot: das Ergebnis ist nicht verwertbar."}.get(f["ampel"], ""))
        sh = f["sharpe_intervall"]
        if sh:
            g.append(f"Das 95-%-Intervall der Sharpe-Ratio reicht von {de(sh['lo'])} bis {de(sh['hi'])}"
                     + (" und liegt über 0." if sh["sig"] else " und schließt 0 ein: ein echter Vorteil ist nicht gesichert."))
        if f["zufallsvergleich_p"] is not None:
            g.append(f"Zufallsvergleich: p = {de(f['zufallsvergleich_p'], 3)}.")
        f["gruende"] = [x for x in g if x]
    elif qid == "costs":
        ct = c.get("cost") or {}
        g = [f"Kosten je Seite: {de(cost_pct, 3)} %, {m.get('trades')} Trades."]
        if ct.get("ok"):
            f["rendite_je_kostenstufe"] = {f"{r['mult']}x": r["total_return_pct"] for r in ct["rows"]}
            f["kosten_bis_gewinn_null_x"] = ct.get("break_even_mult")
            if ct.get("verdict"):
                g.append(ct["verdict"])
        else:
            g.append("Für diesen Lauf gibt es keine Kosten-Sensitivität.")
        f["gruende"] = g
    return f


def ask_rules(f: dict) -> dict:
    punkte = list(f.get("gruende") or [])
    s = f.get("kennzahlen_zeitraum")
    if s:
        head = (f"{f['gewaehlter_zeitraum']} ({s['zeitraum']}): Strategie {de(s['strategie_pct'])} %, Buy & Hold {de(s['buyhold_pct'])} %, "
                f"größter Rückgang im Zeitraum {de(s['max_rueckgang_pct'])} %, {s['trades']} Trades.")
    elif "rueckgang_pct" in f:
        head = (f"Der größte Rückgang war {de(f['rueckgang_pct'])} % vom Hoch am {f['hoch']} bis zum Tief am {f['tief']}"
                + (f"; wieder aufgeholt am {f['erholt_am']}." if f.get("erholt_am") else "; bis zum Ende nicht aufgeholt."))
    elif "rendite_pct" in f:
        head = f"Strategie {de(f['rendite_pct'])} % gegenüber Buy & Hold {de(f['buyhold_pct'])} %."
    elif "ampel" in f:
        head = "So belastbar ist das Ergebnis:"
    else:
        head = "So wirken die Kosten:"
    return {"antwort": head, "punkte": punkte[:5]}


def _parse_ask(text):
    d = _json(text)
    items = d.get("punkte") or []
    out = {"antwort": _clean(d.get("antwort", ""), 700),
           "punkte": [_clean(i, 300) for i in ([items] if isinstance(items, str) else items) if str(i).strip()][:5]}
    if not out["antwort"]:
        raise ValueError("Antwort unvollständig")
    return out


def ask(run, user, qid: str, arg: str = "") -> AiResult:
    """Feste Frage beantworten (gespeichert je Lauf und Frage)."""
    if qid != "period":
        arg = ""
    key = f"{qid}:{arg}" if arg else qid
    existing = _get(user, "ask", run, key=key)
    if existing:
        return existing
    f = ask_facts(run, qid, arg)
    rules = ask_rules(f)
    system = BASE_RULES + """
Aufgabe: Beantworte die Frage im Datenblatt ausschließlich mit den Angaben unter "gruende" und den Kennzahlen. Nenne keine weiteren Ursachen.
Antworte nur als JSON mit den Schlüsseln "antwort" (Text, max. 70 Wörter) und "punkte" (Liste, höchstens 4 kurze Sätze, nur Inhalte aus "gruende")."""
    data, source = llm_rewrite(user, system, f, _parse_ask, lambda d: " ".join([d["antwort"], *d["punkte"]]))
    payload = {"frage": f["frage"], **(data or rules)}
    if not data:
        payload["note"] = source
    return _store(user, "ask", run, key=key, payload=payload, source=source if data else "regeln")
