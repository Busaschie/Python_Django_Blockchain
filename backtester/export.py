"""Export einer Auswertung: Trades als CSV (Excel-tauglich) und eine PDF-Zusammenfassung."""
import csv
import io
from xml.sax.saxutils import escape

from .fmt import de

_REPL = {"≥": ">=", "≤": "<=", "≈": "ca.", "→": "->", "−": "-", "✓": "ok", "⚠": "!", "–": "–", "’": "'", "“": '"', "”": '"'}


def _num(x, nd=2):
    return "" if x is None else f"{x:.{nd}f}".replace(".", ",")


def trades_csv(run) -> bytes:
    """Semikolon-getrennt, Dezimalkomma, UTF-8 mit BOM: öffnet in deutschem Excel per Doppelklick korrekt."""
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\r\n")
    w.writerow(["Kauf", "Kaufpreis", "Verkauf", "Verkaufspreis", "Netto %", "Grund", "Größe %"])
    for t in run.curves.get("trades", []):
        w.writerow([(t.get("entry_ts") or "")[:10], _num(t.get("entry_px"), 6),
                    (t.get("exit_ts") or "offen")[:10], _num(t.get("exit_px"), 6),
                    _num(t.get("ret_pct")), t.get("reason", ""), _num(t.get("size_pct"), 1)])
    return out.getvalue().encode("utf-8-sig")


def _t(s) -> str:
    """Text für die PDF-Standardschrift (Latin-1/WinAnsi): nicht darstellbare Zeichen ersetzen, XML maskieren."""
    s = str(s)
    for a, b in _REPL.items():
        s = s.replace(a, b)
    return escape(s.encode("cp1252", "replace").decode("cp1252"))


def summary_pdf(run) -> bytes:
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.shapes import Drawing
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9, leading=12)
    small = ParagraphStyle("s", parent=body, fontSize=7.5, leading=10, textColor=colors.HexColor("#555555"))
    h1 = ParagraphStyle("h1", parent=ss["Heading1"], fontSize=16, spaceAfter=2)
    h2 = ParagraphStyle("h2", parent=ss["Heading2"], fontSize=11.5, spaceBefore=10, spaceAfter=3, textColor=colors.HexColor("#1f4e6b"))
    P = lambda s, st=body: Paragraph(_t(s), st)

    def table(rows, widths=None, head=True):
        t = Table([[Paragraph(_t(c), body) for c in r] for r in rows], colWidths=widths, hAlign="LEFT")
        st = [("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#bbbbbb")), ("VALIGN", (0, 0), (-1, -1), "TOP"),
              ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
        if head:
            st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e6eef3")))
        t.setStyle(TableStyle(st))
        return t

    m, cv = run.metrics, run.curves
    kind = run.validation.get("kind", "single") if run.validation else "single"
    scope = {"walkforward": "Out-of-Sample gesamt", "split": "Testphase"}.get(kind, "Gesamtzeitraum")
    e = [P(f"{run.get_chain_display()} · {run.strategy_label} · {run.timeframe}", h1),
         P(f"{run.mode_label} · {run.period_label} · Börse {run.exchange_label} ({run.symbol}) · Gebühr {run.fee} · "
           f"Slippage {run.slippage} · Ausführung: {run.execution_label}", small),
         P(f"Parameter: {run.params}" + (f" · Risiko: {run.risk_label}" if run.risk_label else ""), small),
         P(f"Erstellt am {run.created_at:%d.%m.%Y}", small)]

    e.append(P(f"Kennzahlen ({scope})", h2))
    rows = [["Kennzahl", "Wert"],
            ["Rendite Strategie", f"{_num(m.get('total_return_pct'))} %"], ["Rendite Buy & Hold", f"{_num(m.get('buyhold_return_pct'))} %"],
            ["Max Drawdown", f"{_num(m.get('max_drawdown_pct'))} %"], ["Sharpe Ratio", _num(m.get("sharpe"))],
            ["Trades", str(m.get("trades", ""))], ["Win-Rate", f"{_num(m.get('win_rate_pct'))} %"]]
    for label, key, unit in (("CAGR (p. a.)", "cagr_pct", " %"), ("Sortino", "sortino", ""), ("Calmar", "calmar", ""),
                             ("Profit Factor", "profit_factor", ""), ("Zeit im Markt", "time_in_market_pct", " %")):
        if key in m:
            rows.append([label, (_num(m[key]) + unit) if m[key] is not None else "–"])
    e.append(table(rows, [60 * mm, 50 * mm]))
    if run.validation.get("overfit_warning"):
        e.append(P("Overfitting-Warnung: Die Performance bricht auf ungesehenen Daten deutlich ein.", body))

    # Equity-Diagramm (Strategie vs. Buy & Hold), auf ca. 300 Punkte reduziert
    n = len(cv.get("strategy", []))
    if n > 1:
        step = max(1, n // 300)
        d = Drawing(170 * mm, 62 * mm)
        lp = LinePlot(); lp.x, lp.y, lp.width, lp.height = 38, 18, 440, 150
        lp.data = [[(i, v) for i, v in enumerate(cv["strategy"]) if i % step == 0],
                   [(i, v) for i, v in enumerate(cv["buyhold"]) if i % step == 0]]
        lp.lines[0].strokeColor, lp.lines[1].strokeColor = colors.HexColor("#1f78b4"), colors.HexColor("#999999")
        lp.lines[0].strokeWidth = lp.lines[1].strokeWidth = 1.1
        lp.xValueAxis.visible = False
        lp.yValueAxis.labelTextFormat = "%d"; lp.yValueAxis.labels.fontSize = 7
        d.add(lp)
        e += [P("Equity (Start 10.000): blau Strategie, grau Buy & Hold", h2), d]

    pl = cv.get("plaus")
    if pl:
        lvl = {"ok": "in Ordnung", "warn": "mit Hinweisen", "bad": "Probleme"}.get(pl["level"], pl["level"])
        e.append(P(f"Plausibilität: {lvl} ({pl['n_ok']} ok, {pl['n_warn']} Hinweise, {pl['n_bad']} Probleme)", h2))
        issues = [c for c in pl["checks"] if c["level"] != "ok"]
        if issues:
            e.append(table([["Prüfung", "Detail"]] + [[c["title"], c["detail"]] for c in issues], [55 * mm, 115 * mm]))

    mc = cv.get("mc")
    if mc and mc.get("ok"):
        e.append(P("Robustheit (Monte-Carlo)", h2))
        r = [["Wahrscheinlichkeit auf Gewinn", f"{mc['prob_profit']} %"],
             ["Rendite (5 % / Median / 95 %)", f"{mc['return_p5']} % / {mc['return_p50']} % / {mc['return_p95']} %"],
             ["Max Drawdown (Median / 95-%-Fall)", f"{mc['dd_median']} % / {mc['dd_p95']} %"]]
        if mc.get("p_value") is not None:
            r.append(["Besser als Zufall?", f"{'ja' if mc.get('beats_random') else 'nein'} (p = {mc['p_value']})"])
        e.append(table(r, [75 * mm, 85 * mm], head=False))

    rg = cv.get("regimes")
    if rg and rg.get("ok"):
        e.append(P("Marktphasen", h2))
        e.append(table([["Phase", "Anteil", "Strategie", "Buy & Hold", "Trades"]] +
                       [[x["label"], f"{x['share_pct']} %", f"{x['strategy_pct']} %" if x["strategy_pct"] is not None else "–",
                         f"{x['buyhold_pct']} %" if x["buyhold_pct"] is not None else "–", str(x["trades"])] for x in rg["rows"]],
                       [45 * mm, 25 * mm, 30 * mm, 30 * mm, 20 * mm]))
        e.append(P(rg.get("hint", ""), small))

    ct = cv.get("cost")
    if ct and ct.get("ok"):
        e.append(P("Kosten-Sensitivität", h2))
        e.append(P(ct["verdict"]))
        e.append(table([["Kosten", "je Seite", "Rendite", "Max Drawdown", "Sharpe"]] +
                       [[f"{x['mult']}×", f"{x['cost_pct']} %", f"{x['total_return_pct']} %", f"{x['max_drawdown_pct']} %", str(x["sharpe"])]
                        for x in ct["rows"]], [25 * mm, 30 * mm, 35 * mm, 35 * mm, 25 * mm]))

    st = cv.get("stab")
    if st and st.get("ok"):
        e += [P("Parameter-Stabilität", h2), P(st["verdict"]),
              P(f"Raster {st['x_name']} × {st['y_name']}; Rendite in % je Parameterkombination"
                f"{' (Testphase)' if st.get('on_test') else ''}.", small)]
        grid = [[f"{st['y_name']} \\ {st['x_name']}"] + [str(x) for x in st["x"]]]
        for yv, row in zip(st["y"], st["ret"]):
            grid.append([str(yv)] + ["–" if v is None else _num(v) for v in row])
        e.append(table(grid))

    ac = run.ai_comment or {}
    if ac.get("zusammenfassung"):
        e.append(P("Kommentar zur Auswertung", h2))
        e.append(P(ac["zusammenfassung"]))
        for title, key in (("Stärken", "staerken"), ("Schwächen und Einschränkungen", "schwaechen"), ("Nächste Schritte", "naechste_schritte")):
            if ac.get(key):
                e.append(P(title, ParagraphStyle("h3", parent=body, fontName="Helvetica-Bold", spaceBefore=4)))
                e += [P("• " + x) for x in ac[key]]

    e += [Spacer(1, 8), P("Automatisch erzeugte Auswertung auf historischen Daten. Keine Anlageberatung; Ergebnisse der Vergangenheit "
                          "sind keine Garantie für die Zukunft.", small)]
    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                      title=_t(f"Auswertung {run.strategy_label} {run.symbol}")).build(e)
    return buf.getvalue()
