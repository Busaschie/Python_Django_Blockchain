"""KI-Kommentar zu einer Auswertung (nur auf Knopfdruck, einmal je Lauf gespeichert).

Aufbau: Datenblatt aus den bereits berechneten Ergebnissen -> Groq (OpenAI-kompatible API) -> Pruefung der Antwort
-> sonst regelbasierter Kommentar. Das Modell rechnet nichts: Alle Zahlen kommen aus dem Datenblatt und jede Zahl der
Antwort wird dagegen geprueft. Gesendet werden nur Kennzahlen, keine Kursreihen und keine Benutzerdaten."""
import json
import logging
import os
import re
import urllib.error
import urllib.request

from django.utils import timezone

from .fmt import de

log = logging.getLogger("tradebot.ai")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_ITEMS, MAX_LEN = 5, 320

SYSTEM = """Du bist ein sachlicher Assistent in einer Backtest-Software für Kryptowährungs-Strategien.
Du erklärst das Ergebnis einer Auswertung für Laien auf Deutsch und schlägst nächste Tests vor.
Regeln:
- Verwende ausschließlich Zahlen aus dem Datenblatt. Rechne nichts selbst, schätze nichts, runde nicht um.
- Keine Kauf- oder Verkaufsempfehlung, keine Prognose, kein Renditeversprechen. Nenne Ergebnisse ausdrücklich vergangenheitsbezogen.
- Beachte die Plausibilität: Bei Hinweisen oder Problemen (zu wenige Trades, Überanpassung, Datenlücken) sage klar, wie belastbar das Ergebnis ist.
- Nächste Schritte sind ausschließlich Tests, die man in der Software selbst einstellen kann: Zeitraum verlängern, anderes Zeitfenster, Modus (Train/Test-Split, Walk-Forward), Stop-Loss/Take-Profit/Trailing-Stop, Positionsgröße, andere Strategie oder Parameter, höhere Kosten ansetzen, andere Chain oder Börse.
- Kurze, klare Sätze. Keine Markdown-Formatierung.
Antworte nur als JSON-Objekt mit den Schlüsseln:
"zusammenfassung" (Text, maximal 120 Wörter),
"staerken" (Liste mit höchstens 4 kurzen Sätzen),
"schwaechen" (Liste mit höchstens 4 kurzen Sätzen, Risiken und Einschränkungen),
"naechste_schritte" (Liste mit höchstens 4 konkreten Test-Vorschlägen, je mit kurzer Begründung)."""


class AiError(Exception):
    pass


# ---------------------------------------------------------------- Datenblatt
def facts(run) -> dict:
    """Kompaktes Datenblatt aus den fertigen Ergebnissen (alles bereits berechnet und gerundet)."""
    c, m, v = run.curves or {}, run.metrics or {}, run.validation or {}
    f = {
        "strategie": run.strategy_label, "chain": run.symbol, "zeitfenster": run.timeframe, "modus": run.mode_label,
        "zeitraum": run.period_label, "parameter": run.params, "risiko": run.risk_label or "keines",
        "kosten_je_seite_pct": round((run.fee + (run.slippage or 0)) * 100, 4),
        "kennzahlen": {k: m.get(k) for k in (
            "total_return_pct", "buyhold_return_pct", "max_drawdown_pct", "buyhold_max_drawdown_pct", "sharpe", "sortino",
            "calmar", "profit_factor", "cagr_pct", "buyhold_cagr_pct", "trades", "win_rate_pct", "time_in_market_pct")},
    }
    if v.get("kind") == "split":
        f["train_test"] = {"train_sharpe": (v.get("train") or {}).get("sharpe"), "test_sharpe": (v.get("test") or {}).get("sharpe"),
                           "train_return_pct": (v.get("train") or {}).get("total_return_pct"),
                           "test_return_pct": (v.get("test") or {}).get("total_return_pct"),
                           "verdacht_ueberanpassung": bool(v.get("overfit_warning"))}
    elif v.get("kind") == "walkforward":
        f["walk_forward"] = {"profitable_folds": v.get("profitable_folds"), "train_sharpe_mittel": v.get("train_sharpe_mean"),
                             "verdacht_ueberanpassung": bool(v.get("overfit_warning"))}
    pl = c.get("plaus") or {}
    if pl:
        f["plausibilitaet"] = {"gesamt": pl.get("level"),
                               "auffaellig": [f"{x['title']}: {x['detail']}" for x in pl.get("checks", []) if x["level"] != "ok"]}
    mc = c.get("mc") or {}
    if mc.get("ok"):
        f["monte_carlo"] = {k: mc.get(k) for k in ("n_trades", "prob_profit", "return_p5", "return_p50", "return_p95",
                                                   "dd_median", "dd_p95", "random_median", "p_value", "beats_random")}
    rg = c.get("regimes") or {}
    if rg.get("ok"):
        f["marktphasen"] = {"phasen": [{k: r[k] for k in ("label", "share_pct", "strategy_pct", "buyhold_pct", "trades")}
                                       for r in rg["rows"]], "fazit": rg.get("hint")}
    ct = c.get("cost") or {}
    if ct.get("ok"):
        f["kosten_test"] = {"rendite_je_kostenstufe": {f"{r['mult']}x": r["total_return_pct"] for r in ct["rows"]},
                            "kosten_bis_gewinn_null_x": ct.get("break_even_mult"), "fazit": ct.get("verdict")}
    return f


def facts_text(f: dict) -> str:
    return json.dumps(f, ensure_ascii=False, separators=(",", ":"), default=str)


# ---------------------------------------------------------------- Pruefung der Antwort
_NUM = re.compile(r"\d+(?:[.,]\d+)?")
_FREE = {20, 30, 90, 100, 180, 365, 1000}    # haeufige runde Zahlen aus Regeln/Erklaerungen


def _nums(text: str):
    for m in _NUM.finditer(text):
        s = m.group(0).replace(",", ".")
        yield float(s), (len(s.split(".")[1]) if "." in s else 0)


def unknown_numbers(answer: str, fact_text: str) -> list:
    known = [x for x, _ in _nums(fact_text)]
    bad = []
    for x, d in _nums(answer):
        if (x == int(x) and x <= 10) or x in _FREE:
            continue
        if any(abs(k - x) <= 0.5 * 10 ** -d + 1e-9 for k in known):
            continue
        bad.append(x)
    return bad


def parse_answer(text: str) -> dict:
    """JSON aus der Antwort lesen und in feste Form bringen; ValueError, wenn unbrauchbar."""
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text or "", re.S)
        if not m:
            raise ValueError("keine JSON-Antwort")
        data = json.loads(m.group(0))
    clean = lambda s: re.sub(r"[*_`#]+", "", str(s)).strip()[:MAX_LEN * 2]  # noqa: E731
    out = {"zusammenfassung": clean(data.get("zusammenfassung", ""))}
    for k in ("staerken", "schwaechen", "naechste_schritte"):
        items = data.get(k) or []
        if isinstance(items, str):
            items = [items]
        out[k] = [clean(i)[:MAX_LEN] for i in items if str(i).strip()][:MAX_ITEMS]
    if not out["zusammenfassung"] or not out["naechste_schritte"]:
        raise ValueError("Antwort unvollständig")
    return out


def answer_text(c: dict) -> str:
    return " ".join([c["zusammenfassung"], *c["staerken"], *c["schwaechen"], *c["naechste_schritte"]])


# ---------------------------------------------------------------- Groq
def chat(model: str, messages: list) -> str:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise AiError("GROQ_API_KEY fehlt")
    body = {"model": model, "messages": messages, "temperature": 0.2, "max_completion_tokens": 2500,
            "response_format": {"type": "json_object"}}
    if "gpt-oss" in model:
        body.update(reasoning_effort="low", include_reasoning=False)
    req = urllib.request.Request(GROQ_URL, json.dumps(body).encode(), {
        "Content-Type": "application/json", "Authorization": f"Bearer {key}", "User-Agent": "tradebot/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            data = json.loads(r.read().decode())
        return data["choices"][0]["message"]["content"] or ""
    except urllib.error.HTTPError as e:
        raise AiError(f"Groq {e.code}: {e.read().decode(errors='replace')[:200]}") from None
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as e:
        raise AiError(f"Groq nicht erreichbar oder unerwartete Antwort: {e}") from None


def ask_llm(f: dict) -> tuple:
    """(Kommentar, Modellname). Probiert Hauptmodell, dann Ausweichmodell; bei unbekannten Zahlen eine Korrektur-Runde."""
    text = facts_text(f)
    models = [os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"), os.environ.get("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")]
    last = "kein Versuch"
    for model in dict.fromkeys(models):
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "Datenblatt:\n" + text}]
        for attempt in range(2):
            try:
                answer = chat(model, messages)
                comment = parse_answer(answer)
            except (AiError, ValueError) as exc:
                last = f"{model}: {exc}"
                break      # naechstes Modell
            bad = unknown_numbers(answer_text(comment), text)
            if not bad:
                return comment, model
            last = f"{model}: Zahlen nicht im Datenblatt: {bad[:5]}"
            messages += [{"role": "assistant", "content": answer},
                         {"role": "user", "content": "Diese Zahlen stehen nicht im Datenblatt: "
                          + ", ".join(str(b) for b in bad[:6]) + ". Verwende nur Zahlen aus dem Datenblatt. Antworte erneut als JSON."}]
    raise AiError(last)


# ---------------------------------------------------------------- Regelbasierter Kommentar (Ausweichlösung)
def rules_comment(f: dict) -> dict:
    k = f["kennzahlen"]
    ret, bh, dd, bdd = k.get("total_return_pct"), k.get("buyhold_return_pct"), k.get("max_drawdown_pct"), k.get("buyhold_max_drawdown_pct")
    trades = k.get("trades") or 0
    rel = ("vor" if ret is not None and bh is not None and ret > bh else "hinter")
    pl = f.get("plausibilitaet") or {}
    summary = (f"{f['strategie']} auf {f['chain']} ({f['zeitfenster']}, {f['modus']}, {f['zeitraum']}): "
               f"Rendite {de(ret)} % gegenüber {de(bh)} % bei Buy & Hold, größter Rückgang {de(dd)} %, {trades} Trade{'s' if trades != 1 else ''}. "
               f"Die Strategie liegt {rel} Buy & Hold.")
    if pl.get("gesamt") in ("warn", "bad"):
        summary += " Die Plausibilitäts-Ampel zeigt Hinweise, das Ergebnis ist nur eingeschränkt belastbar."
    strong, weak, steps = [], [], []
    if ret is not None and bh is not None and ret > bh:
        strong.append(f"Rendite über Buy & Hold ({de(ret)} % gegenüber {de(bh)} %).")
    if dd is not None and bdd is not None and abs(dd) < abs(bdd):
        strong.append(f"Geringerer Rückgang als Buy & Hold ({de(dd)} % gegenüber {de(bdd)} %).")
    mc = f.get("monte_carlo") or {}
    if mc.get("beats_random"):
        strong.append(f"Besser als zufällige Einstiege (p = {de(mc['p_value'], 3)}).")
    ct = f.get("kosten_test") or {}
    if ct.get("kosten_bis_gewinn_null_x") is None and ct:
        strong.append("Das Ergebnis bleibt auch bei deutlich höheren Kosten positiv.")
    for line in (pl.get("auffaellig") or [])[:3]:
        weak.append(line)
    if (f.get("train_test") or {}).get("verdacht_ueberanpassung") or (f.get("walk_forward") or {}).get("verdacht_ueberanpassung"):
        weak.append("Verdacht auf Überanpassung: außerhalb des Trainings schneidet die Strategie deutlich schlechter ab.")
    rg = f.get("marktphasen") or {}
    if rg.get("fazit"):
        weak.append(rg["fazit"])
    be = ct.get("kosten_bis_gewinn_null_x") if ct else None
    if ct and be is not None and be < 2:
        weak.append(ct.get("fazit", ""))
    if trades < 30:
        steps.append("Zeitraum verlängern oder ein kleineres Zeitfenster wählen, um mehr Trades und damit eine belastbarere Aussage zu bekommen.")
    if f["modus"].lower().startswith("einzel") or "gesamtzeitraum" in f["modus"].lower():
        steps.append("Mit Train/Test-Split oder Walk-Forward gegenprüfen, ob das Ergebnis auch außerhalb der Optimierung hält.")
    if f["risiko"] == "keines":
        steps.append("Stop-Loss oder Trailing-Stop testen, um den größten Rückgang zu begrenzen.")
    if ct and be is not None and be < 2:
        steps.append("Weniger handeln (größeres Zeitfenster oder längere Indikator-Perioden), damit Kosten weniger ins Gewicht fallen.")
    if rg and any(p["label"] == "Abwärts" and (p["strategy_pct"] or 0) < 0 for p in rg.get("phasen", [])):
        steps.append("Die Strategie „Kombiniert“ mit Trendfilter testen, um Verluste in Abwärtsphasen zu verringern.")
    steps.append("Dieselben Einstellungen auf den anderen Chains und mit einer anderen Börse vergleichen.")
    return {"zusammenfassung": summary, "staerken": strong[:4] or ["Keine klaren Stärken erkennbar."],
            "schwaechen": [w for w in weak if w][:4] or ["Keine besonderen Auffälligkeiten."], "naechste_schritte": steps[:4]}


# ---------------------------------------------------------------- Limits und Ablauf
def limits() -> tuple:
    return int(os.environ.get("AI_USER_DAILY_LIMIT", "20")), int(os.environ.get("AI_GLOBAL_DAILY_LIMIT", "150"))


def used_today(user=None) -> int:
    from .models import AiCall
    day_start = timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
    qs = AiCall.objects.filter(created_at__gte=day_start)
    return (qs.filter(user=user) if user is not None else qs).count()


def remaining_today(user) -> int:
    per_user, global_ = limits()
    return max(0, min(per_user - used_today(user), global_ - used_today()))


def generate(run, user) -> None:
    """Kommentar erzeugen und im Lauf speichern (KI, sonst Regeln). Speichert auch einen Hinweis zur Quelle."""
    from .models import AiCall
    f = facts(run)
    note, source, comment = "", "regeln", None
    if not os.environ.get("GROQ_API_KEY"):
        note = "KI nicht eingerichtet (GROQ_API_KEY fehlt), regelbasierter Kommentar."
    elif remaining_today(user) <= 0:
        note = f"Tageslimit für KI-Kommentare erreicht ({limits()[0]} je Benutzer), regelbasierter Kommentar."
    else:
        call = AiCall.objects.create(user=user)
        try:
            comment, model = ask_llm(f)
            source, call.ok, call.model = f"groq:{model}", True, model
        except AiError as exc:
            log.warning("KI-Kommentar fehlgeschlagen: %s", exc)
            note = "KI momentan nicht verfügbar oder Antwort nicht verwertbar, regelbasierter Kommentar."
        call.save()
    comment = comment or rules_comment(f)
    run.ai_comment = {**comment, "note": note}
    run.ai_source, run.ai_created = source, timezone.now()
    run.save(update_fields=["ai_comment", "ai_source", "ai_created"])
