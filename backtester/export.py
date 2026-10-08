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


ACCENT, INK, MUTED = "#1f4e6b", "#222222", "#666666"
GOOD, WARN, BAD = "#cfeedd", "#fbeab0", "#f6c9c9"          # Zellfarben (hell, drucktauglich)
LEVEL_BG = {"ok": GOOD, "warn": WARN, "bad": BAD}
LEVEL_TXT = {"ok": "in Ordnung", "warn": "Hinweise", "bad": "Probleme"}
PALETTE = ["#1f78b4", "#e8590c", "#2b8a3e", "#9c36b5", "#c92a2a", "#0b7285", "#e67700", "#495057"]
MAX_REPORT_RUNS = 8


def _kind(run) -> str:
    return run.validation.get("kind", "single") if run.validation else "single"


def _scope(run) -> str:
    return {"walkforward": "Out-of-Sample gesamt", "split": "Testphase"}.get(_kind(run), "Gesamtzeitraum")


def _short_title(run) -> str:
    return f"{run.get_chain_display()} · {run.strategy_label} · {run.timeframe}"


def _styles():
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    ss = getSampleStyleSheet()
    body = ParagraphStyle("b", parent=ss["BodyText"], fontSize=9, leading=12, textColor=colors.HexColor(INK))
    return {
        "body": body,
        "small": ParagraphStyle("s", parent=body, fontSize=7.5, leading=10, textColor=colors.HexColor(MUTED)),
        "cell": ParagraphStyle("c", parent=body, fontSize=8, leading=10),
        "h1": ParagraphStyle("h1", parent=ss["Heading1"], fontSize=17, leading=20, spaceAfter=2, textColor=colors.HexColor(ACCENT)),
        "h2": ParagraphStyle("h2", parent=ss["Heading2"], fontSize=11.5, spaceBefore=11, spaceAfter=3, textColor=colors.HexColor(ACCENT)),
        "h3": ParagraphStyle("h3", parent=body, fontName="Helvetica-Bold", spaceBefore=4),
        "tile_v": ParagraphStyle("tv", parent=body, fontName="Helvetica-Bold", fontSize=15, leading=18, alignment=1),
        "tile_l": ParagraphStyle("tl", parent=body, fontSize=7.5, leading=9, alignment=1, textColor=colors.HexColor(MUTED)),
    }


def _table(rows, widths=None, head=True, style="cell", bg=None, S=None):
    """Tabelle mit Kopfzeile; bg = {(spalte, zeile): Farbe} faerbt einzelne Zellen."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    S = S or _styles()
    t = Table([[c if not isinstance(c, str) else Paragraph(_t(c), S[style]) for c in r] for r in rows],
              colWidths=widths, hAlign="LEFT", repeatRows=1 if head else 0)
    st = [("GRID", (0, 0), (-1, -1), .3, colors.HexColor("#c9d3da")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
          ("TOPPADDING", (0, 0), (-1, -1), 2.5), ("BOTTOMPADDING", (0, 0), (-1, -1), 2.5)]
    if head:
        st.append(("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e3edf3")))
    for (c, r), color in (bg or {}).items():
        st.append(("BACKGROUND", (c, r), (c, r), colors.HexColor(color)))
    t.setStyle(TableStyle(st))
    return t


def _kpi_tiles(run, S, width):
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Table, TableStyle
    m = run.metrics
    ret = m.get("total_return_pct")
    tiles = [("Rendite Strategie", f"{_num(ret)} %", GOOD if (ret or 0) >= 0 else BAD),
             ("Rendite Buy & Hold", f"{_num(m.get('buyhold_return_pct'))} %", "#eef2f5"),
             ("Max Drawdown", f"{_num(m.get('max_drawdown_pct'))} %", "#eef2f5"),
             ("Sharpe Ratio", _num(m.get("sharpe")), "#eef2f5")]
    t = Table([[Paragraph(_t(v), S["tile_v"]) for _, v, _ in tiles], [Paragraph(_t(l), S["tile_l"]) for l, _, _ in tiles]],
              colWidths=[width / 4] * 4)
    st = [("TOPPADDING", (0, 0), (-1, 0), 7), ("BOTTOMPADDING", (0, 1), (-1, 1), 6), ("BOX", (0, 0), (-1, -1), .3, colors.white),
          ("LINEAFTER", (0, 0), (-2, -1), 3, colors.white)]
    for i, (_, _, bg) in enumerate(tiles):
        st.append(("BACKGROUND", (i, 0), (i, 1), colors.HexColor(bg)))
    t.setStyle(TableStyle(st))
    return t


def _ts(s):
    from datetime import datetime
    return datetime.fromisoformat(str(s)[:19])


def _curve(run, key="strategy", normalize=True):
    """(Tage seit Epoche, Werte) einer Kurve; bei Train/Test nur die Testphase; Start auf 100 normiert."""
    cv = run.curves
    idx, vals = cv.get("index") or [], cv.get(key) or []
    cut = run.validation.get("split_at") if _kind(run) == "split" else None
    pts = [(i, v) for i, v in zip(idx, vals) if v is not None and (not cut or i >= cut)]
    if len(pts) < 2:
        return [], []
    xs = [_ts(i).toordinal() + _ts(i).hour / 24 for i, _ in pts]
    base = pts[0][1] or 1
    ys = [v / base * 100 if normalize else v for _, v in pts]
    return xs, ys


def _line_chart(series, width_mm=170, height_mm=62, ylabel_fmt="%d"):
    """series = [(name, farbe, xs, ys, dashed)]; liefert ein Drawing mit Achsenwerten, Datumsmarken und Legende."""
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.shapes import Drawing, Line, String
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    series = [x for x in series if x[2]]
    if not series:
        return None
    d = Drawing(width_mm * mm, height_mm * mm)
    lp = LinePlot()
    lp.x, lp.y, lp.width, lp.height = 38, 26, width_mm * mm - 50, height_mm * mm - 44
    step = lambda n: max(1, n // 300)   # noqa: E731  (auf ca. 300 Punkte je Linie reduzieren)
    lp.data = [list(zip(xs, ys))[::step(len(xs))] for _, _, xs, ys, _ in series]
    for i, (_, color, _, _, dashed) in enumerate(series):
        lp.lines[i].strokeColor, lp.lines[i].strokeWidth = colors.HexColor(color), 1.2
        if dashed:
            lp.lines[i].strokeDashArray = (3, 2)
    lp.xValueAxis.visible = False
    lp.yValueAxis.labelTextFormat = ylabel_fmt
    lp.yValueAxis.labels.fontSize = 7
    lp.yValueAxis.gridStrokeColor, lp.yValueAxis.gridStrokeWidth = colors.HexColor("#dfe6eb"), .4
    lp.yValueAxis.visibleGrid = True
    d.add(lp)
    lo = min(x[2][0] for x in series)
    hi = max(x[2][-1] for x in series)
    from datetime import date
    for xval, anchor, px in ((lo, "start", lp.x), (hi, "end", lp.x + lp.width)):
        d.add(String(px, 16, date.fromordinal(int(xval)).strftime("%d.%m.%Y"), fontSize=7, fillColor=colors.HexColor(MUTED), textAnchor=anchor))
    x = lp.x
    for name, color, _, _, dashed in series:      # Legende unter dem Diagramm
        d.add(Line(x, 5, x + 12, 5, strokeColor=colors.HexColor(color), strokeWidth=1.6, strokeDashArray=(3, 2) if dashed else None))
        label = _t(name).replace("&amp;", "&")
        d.add(String(x + 15, 2.5, label, fontSize=7, fillColor=colors.HexColor(INK)))
        x += 15 + 4.3 * len(label) + 12
    return d


def _heat_color(v, vmax):
    if v is None:
        return "#f4f4f4"
    a = min(1.0, abs(v) / (vmax or 1))
    r, g, b = (0xcf, 0xee, 0xdd) if v >= 0 else (0xf6, 0xc9, 0xc9)
    mix = lambda c: int(255 - (255 - c) * (0.35 + 0.65 * a))   # noqa: E731
    return "#%02x%02x%02x" % (mix(r), mix(g), mix(b))


def _run_story(run, S, W):
    """Alle Abschnitte einer Auswertung als reportlab-Bausteine (W = nutzbare Breite in Punkt)."""
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, Spacer
    P = lambda s, st="body": Paragraph(_t(s), S[st])   # noqa: E731
    m, cv = run.metrics, run.curves
    e = [P(_short_title(run), "h1"),
         HRFlowable(width="100%", thickness=1.2, color=colors.HexColor(ACCENT), spaceAfter=4),
         P(f"{run.mode_label} · {run.period_label} · Börse {run.exchange_label} ({run.symbol}) · Gebühr {run.fee} · "
           f"Slippage {run.slippage} · Ausführung: {run.execution_label}", "small"),
         P(f"Parameter: {run.params}" + (f" · Risiko: {run.risk_label}" if run.risk_label else ""), "small"),
         P(f"Erstellt am {run.created_at:%d.%m.%Y}" + (f" · Tags: {', '.join(run.tag_list)}" if run.tag_list else ""), "small"),
         Spacer(1, 6), _kpi_tiles(run, S, W), P(f"Kennzahlen: {_scope(run)}", "small")]

    rows = [["Kennzahl", "Wert"], ["Trades", str(m.get("trades", ""))], ["Win-Rate", f"{_num(m.get('win_rate_pct'))} %"]]
    for label, key, unit in (("CAGR (p. a.)", "cagr_pct", " %"), ("Sortino", "sortino", ""), ("Calmar", "calmar", ""),
                             ("Profit Factor", "profit_factor", ""), ("Zeit im Markt", "time_in_market_pct", " %")):
        if key in m:
            rows.append([label, (_num(m[key]) + unit) if m[key] is not None else "–"])
    e += [Spacer(1, 4), _table(rows, [60 * mm, 40 * mm], S=S)]
    if run.validation.get("overfit_warning"):
        e.append(P("Overfitting-Warnung: Die Performance bricht auf ungesehenen Daten deutlich ein.", "body"))

    xs, ys = _curve(run, "strategy", normalize=False)
    bx, by = _curve(run, "buyhold", normalize=False)
    chart = _line_chart([("Strategie", PALETTE[0], xs, ys, False), ("Buy & Hold", "#868e96", bx, by, True)], W / mm)
    if chart is not None:
        title = "Equity in der Testphase (die Gesamtkurve startet bei 10.000)" if _kind(run) == "split" else "Equity (Start 10.000)"
        e.append(KeepTogether([P(title, "h2"), chart]))

    pl = cv.get("plaus")
    if pl:
        e.append(P(f"Plausibilität: {LEVEL_TXT.get(pl['level'], pl['level'])} ({pl['n_ok']} ok, {pl['n_warn']} Hinweise, {pl['n_bad']} Probleme)", "h2"))
        issues = [c for c in pl["checks"] if c["level"] != "ok"]
        if issues:
            rows = [["", "Prüfung", "Detail"]] + [["", c["title"], c["detail"]] for c in issues]
            bg = {(0, i + 1): LEVEL_BG[c["level"]] for i, c in enumerate(issues)}
            e.append(_table(rows, [6 * mm, 50 * mm, W - 56 * mm], bg=bg, S=S))

    mc = cv.get("mc")
    if mc and mc.get("ok"):
        r = [["Wahrscheinlichkeit auf Gewinn", f"{mc['prob_profit']} %"],
             ["Rendite (5 % / Median / 95 %)", f"{mc['return_p5']} % / {mc['return_p50']} % / {mc['return_p95']} %"],
             ["Max Drawdown (Median / 95-%-Fall)", f"{mc['dd_median']} % / {mc['dd_p95']} %"]]
        if mc.get("p_value") is not None:
            r.append(["Besser als Zufall?", f"{'ja' if mc.get('beats_random') else 'nein'} (p = {mc['p_value']})"])
        e.append(KeepTogether([P("Robustheit (Monte-Carlo)", "h2"), _table(r, [75 * mm, 85 * mm], head=False, S=S)]))

    rg = cv.get("regimes")
    if rg and rg.get("ok"):
        tbl = _table([["Phase", "Anteil", "Strategie", "Buy & Hold", "Trades"]] +
                     [[x["label"], f"{x['share_pct']} %", f"{x['strategy_pct']} %" if x["strategy_pct"] is not None else "–",
                       f"{x['buyhold_pct']} %" if x["buyhold_pct"] is not None else "–", str(x["trades"])] for x in rg["rows"]],
                     [45 * mm, 25 * mm, 30 * mm, 30 * mm, 20 * mm], S=S)
        e.append(KeepTogether([P("Marktphasen", "h2"), tbl, P(rg.get("hint", ""), "small")]))

    ct = cv.get("cost")
    if ct and ct.get("ok"):
        tbl = _table([["Kosten", "je Seite", "Rendite", "Max Drawdown", "Sharpe"]] +
                     [[f"{x['mult']}×", f"{x['cost_pct']} %", f"{x['total_return_pct']} %", f"{x['max_drawdown_pct']} %", str(x["sharpe"])]
                      for x in ct["rows"]], [25 * mm, 30 * mm, 35 * mm, 35 * mm, 25 * mm], S=S,
                     bg={(2, i + 1): BAD for i, x in enumerate(ct["rows"]) if x["total_return_pct"] < 0})
        e.append(KeepTogether([P("Kosten-Sensitivität", "h2"), P(ct["verdict"]), tbl]))

    st = cv.get("stab")
    if st and st.get("ok"):
        vmax = max([abs(v) for row in st["ret"] for v in row if v is not None] or [1])
        grid = [[f"{st['y_name']} \\ {st['x_name']}"] + [str(x) for x in st["x"]]]
        bg = {}
        for j, (yv, row) in enumerate(zip(st["y"], st["ret"])):
            grid.append([str(yv)] + ["–" if v is None else _num(v) for v in row])
            for i, v in enumerate(row):
                bg[(i + 1, j + 1)] = _heat_color(v, vmax)
        e.append(KeepTogether([P("Parameter-Stabilität", "h2"), P(st["verdict"]),
                               P(f"Rendite in % je Parameterkombination{' (Testphase)' if st.get('on_test') else ''}; grün = Gewinn, rot = Verlust.", "small"),
                               _table(grid, bg=bg, S=S)]))

    ac = run.ai_comment or {}
    if ac.get("zusammenfassung"):
        e += [P("Kommentar zur Auswertung", "h2"), P(ac["zusammenfassung"])]
        for title, key in (("Stärken", "staerken"), ("Schwächen und Einschränkungen", "schwaechen"), ("Nächste Schritte", "naechste_schritte")):
            if ac.get(key):
                e.append(P(title, "h3"))
                e += [P("• " + x) for x in ac[key]]
    return e


def _build(story, title):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.platypus import SimpleDocTemplate
    from datetime import date

    def decorate(canvas, doc):
        w, h = A4
        canvas.saveState()
        canvas.setFillColor(colors.HexColor(ACCENT)); canvas.rect(0, h - 9 * mm, w, 9 * mm, stroke=0, fill=1)
        canvas.setFillColor(colors.white); canvas.setFont("Helvetica-Bold", 8.5)
        canvas.drawString(18 * mm, h - 6 * mm, "Trading Backtester")
        canvas.setFont("Helvetica", 8); canvas.drawRightString(w - 18 * mm, h - 6 * mm, date.today().strftime("%d.%m.%Y"))
        canvas.setFillColor(colors.HexColor(MUTED)); canvas.setFont("Helvetica", 7.5)
        canvas.drawCentredString(w / 2, 9 * mm, f"Keine Anlageberatung · Ergebnisse der Vergangenheit sind keine Garantie · Seite {doc.page}")
        canvas.restoreState()

    buf = io.BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=16 * mm,
                      title=_t(title), author="Trading Backtester").build(story, onFirstPage=decorate, onLaterPages=decorate)
    return buf.getvalue()


def _width():
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    return A4[0] - 36 * mm


def summary_pdf(run) -> bytes:
    """PDF-Auswertung eines einzelnen Laufs."""
    from reportlab.platypus import Paragraph, Spacer
    S, W = _styles(), _width()
    story = _run_story(run, S, W)
    story += [Spacer(1, 8), Paragraph(_t("Automatisch erzeugte Auswertung auf historischen Daten."), S["small"])]
    return _build(story, f"Auswertung {run.strategy_label} {run.symbol}")


def report_pdf(runs) -> bytes:
    """Bericht mit mehreren Läufen: Übersicht, gemeinsames Diagramm (Start = 100), danach je Lauf die Einzelauswertung."""
    from reportlab.lib.units import mm
    from reportlab.platypus import KeepTogether, PageBreak, Paragraph, Spacer
    runs = list(runs)[:MAX_REPORT_RUNS]
    S, W = _styles(), _width()
    P = lambda s, st="body": Paragraph(_t(s), S[st])   # noqa: E731
    from datetime import date
    story = [P("Auswertungsbericht", "h1"), P(f"{len(runs)} Lauf{'e' if len(runs) != 1 else ''} · erstellt am {date.today():%d.%m.%Y}", "small"),
             P("Übersicht", "h2")]
    rows = [["Nr.", "Auswertung", "Zeitraum", "Rendite %", "B&H %", "Max DD %", "Sharpe", "Trades", "Plausib.", "Stabilität"]]
    bg = {}
    for i, r in enumerate(runs, 1):
        pl, stab = (r.curves or {}).get("plaus"), (r.curves or {}).get("stab") or {}
        m = r.metrics
        rows.append([str(i), f"{_short_title(r)}<br/>{r.mode_label}" + (f" · {', '.join(r.tag_list)}" if r.tag_list else ""),
                     f"{r.start_date:%d.%m.%y}–{r.end_date:%d.%m.%y}" if r.start_date and r.end_date else f"{r.days} Tage",
                     _num(m.get("total_return_pct")), _num(m.get("buyhold_return_pct")), _num(m.get("max_drawdown_pct")),
                     _num(m.get("sharpe")), str(m.get("trades", "")),
                     LEVEL_TXT.get(pl["level"], "–") if pl else "–", LEVEL_TXT.get(stab.get("level"), "–") if stab.get("ok") else "–"])
        if (m.get("total_return_pct") or 0) < 0:
            bg[(3, i)] = BAD
        if pl:
            bg[(8, i)] = LEVEL_BG.get(pl["level"], "#ffffff")
        if stab.get("ok"):
            bg[(9, i)] = LEVEL_BG.get(stab.get("level"), "#ffffff")
    t = _table([[c.replace("<br/>", " · ") for c in row] for row in rows],
               [7 * mm, 38 * mm, 20 * mm, 15 * mm, 13 * mm, 15 * mm, 14 * mm, 13 * mm, 20 * mm, 19 * mm], bg=bg, S=S)
    story.append(t)
    series = []
    for i, r in enumerate(runs):
        xs, ys = _curve(r, "strategy")
        series.append((f"{i + 1} {_short_title(r)}", PALETTE[i % len(PALETTE)], xs, ys, False))
    chart = _line_chart(series, W / mm, 80)
    if chart is not None and len([s for s in series if s[2]]) > 1:
        story.append(KeepTogether([P("Equity im Vergleich (Start = 100)", "h2"), chart,
                                   P("Bei Train/Test-Läufen nur die Testphase. Läufe mit verschiedenen Zeiträumen starten jeweils bei 100.", "small")]))
    for i, r in enumerate(runs, 1):
        story += [PageBreak(), P(f"Lauf {i} von {len(runs)}", "small")] + _run_story(r, S, W)
    story += [Spacer(1, 8), P("Automatisch erzeugte Auswertung auf historischen Daten.", "small")]
    return _build(story, "Auswertungsbericht")
