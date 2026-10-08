"""Tests: Sprachumschaltung DE/EN, Übersetzungs-Engine und Abdeckung (keine deutschen Reste in der englischen Ausgabe)."""
import os
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from config import i18n, i18n_check
from .models import BacktestRun
from .test_extras import computed_run

User = get_user_model()
PW = "Sehr-gutes-Pw-17"
DUMP = os.environ.get("I18N_DUMP")           # Entwicklungshilfe: Liste der fehlenden Texte in diese Datei schreiben


def world(cls):
    """Läufe in allen Betriebsarten, damit viele Textvarianten erscheinen."""
    cls.user = User.objects.create_user("me@x.de", "me@x.de", PW)
    cls.runs = {}
    for key, args in {"single": ("single", "sma_cross"), "split": ("split", "rsi"), "wf": ("walkforward", "combo"),
                      "boll": ("split", "bollinger"), "macd": ("single", "macd"), "don": ("single", "donchian"),
                      "mom": ("single", "momentum")}.items():
        cls.runs[key], _ = computed_run(*args, owner=cls.user)
    cls.runs["risk"], _ = computed_run("single", "sma_cross", owner=cls.user, stop_loss=3, take_profit=6)


class EngineTests(SimpleTestCase):
    def setUp(self):
        i18n._cat["exact"] = None
        self.cat = {"Hallo Welt": "Hello world", "Rendite {} %": "Return {} %", "{} von {} Läufen": "{1} of {0} runs"}
        self.p = mock.patch.object(i18n, "_load", self._load)
        self.p.start()
        i18n._tr_core.cache_clear()
        self._load()

    def tearDown(self):
        self.p.stop()
        i18n._cat["exact"] = None
        i18n._tr_core.cache_clear()

    def _load(self):
        import re as _re
        exact, pats = {}, []
        for de, en in self.cat.items():
            (pats if "{" in de else []).append((de, en)) if "{" in de else exact.__setitem__(de, en)
        parts, tpl = [], []
        for i, (key, en) in enumerate(pats):
            bits = _re.split(r"\{\d*\}", key)
            out = ""
            for k, b in enumerate(bits):
                out += _re.escape(b)
                if k < len(bits) - 1:
                    out += f"(?P<g{i}_{k}>.+?)"
            parts.append(f"(?P<p{i}>{out})")
            tpl.append((en, len(bits) - 1))
        i18n._cat.update(exact=exact, rx=_re.compile("|".join(parts), _re.S), tpl=tpl)

    def test_exact_pattern_and_numbers(self):
        self.assertEqual(i18n.tr("Hallo Welt"), "Hello world")
        self.assertEqual(i18n.tr("  Hallo   Welt\n"), "  Hello world\n")
        self.assertEqual(i18n.tr("Rendite 12,5 %"), "Return 12.5 %")
        self.assertEqual(i18n.tr("3 von 5 Läufen"), "5 of 3 runs")
        self.assertEqual(i18n.tr("Unbekannter Text"), "Unbekannter Text")

    def test_html_text_attr_script_json(self):
        html = ('<html lang="de"><p title="Hallo Welt">Hallo Welt &amp; <b>Rendite 1,5 %</b></p>'
                '<script>var a = "Hallo Welt"; var b = \'x\';</script>'
                '<script id="d" type="application/json">{"k": ["Hallo Welt", 3, "Rendite 2,5 %"]}</script>'
                '<textarea>Hallo Welt</textarea><style>.a{content:"Hallo Welt"}</style></html>')
        out = i18n.translate_html(html)
        self.assertIn('lang="en"', out)
        self.assertIn('title="Hello world"', out)
        self.assertIn("<p title=\"Hello world\">Hallo Welt &amp; <b>Return 1.5 %</b></p>", out)      # Satzteil ohne Eintrag bleibt
        self.assertIn('var a = "Hello world"', out)
        self.assertIn('"Hello world", 3, "Return 2.5 %"', out)
        self.assertIn("<textarea>Hallo Welt</textarea>", out)
        self.assertIn('content:"Hallo Welt"', out)


class DetectorTests(SimpleTestCase):
    def test_looks_german(self):
        self.assertTrue(i18n_check.looks_german("Die Rendite ist hoch"))
        self.assertTrue(i18n_check.looks_german("Prüfung"))
        self.assertFalse(i18n_check.looks_german("Return over the period"))
        self.assertFalse(i18n_check.looks_german("Sharpe"))


@override_settings(SIGNALS_ENABLED=True)
class CoverageTests(TestCase):
    """Rendert die Seiten mit echten Läufen auf Englisch: es darf kein deutscher Text übrig bleiben."""

    @classmethod
    def setUpTestData(cls):
        world(cls)
        a, b = cls.runs["single"], cls.runs["macd"]
        a.batch = b.batch = "bt1"
        a.share_token = "tok123"
        a.tags = "test,eins"
        a.save()
        b.save()
        from . import ai_extra
        ai_extra.compare_runs(cls.runs["single"], cls.runs["split"], cls.user)

    def setUp(self):
        self.client.force_login(self.user)
        self.client.cookies["tb_lang"] = "en"
        from . import paper
        self.df = computed_run("single", "sma_cross")[1]
        p = mock.patch.object(paper, "recent_df", return_value=self.df)
        p.start()
        self.addCleanup(p.stop)
        p2 = mock.patch("backtester.signals.fetch_ohlcv", return_value=(self.df, {}))
        p2.start()
        self.addCleanup(p2.stop)

    def pages(self):
        c, r = self.client, self.runs
        out = {"index": c.get("/")}
        for k, run in r.items():
            out[f"run:{k}"] = c.get(reverse("detail", args=[run.pk]))
        out["compare"] = c.get(reverse("compare", args=["bt1"]))
        out["shared"] = c.get(reverse("shared", args=["tok123"]))
        out["ai_compare"] = c.get(reverse("ai_compare"), {"run": [r["single"].pk, r["split"].pk]})
        return out

    def flows(self):
        c, r = self.client, self.runs
        out = {}
        run = r["single"]
        run.source = "ccxt"
        c.post(reverse("ai_suggest", args=[run.pk]))
        for q in ("costs", "trades", "risk", "period"):
            c.post(reverse("ai_ask", args=[run.pk]), {"q": q, "period": "2025"})
        c.post(reverse("signal_refresh", args=[run.pk]))
        out["run+ai"] = c.get(reverse("detail", args=[run.pk]), follow=True)
        c.post(reverse("signal_toggle", args=[run.pk]))
        out["signal-toggle"] = c.get(reverse("detail", args=[run.pk]))
        c.post(reverse("paper_start", args=[run.pk]), {"capital": "5000"})
        from .models import PaperAccount
        acc = PaperAccount.objects.filter(owner=self.user).first()
        if acc:
            c.post(reverse("paper_check", args=[acc.pk]))
            out["paper_detail"] = c.get(reverse("paper_detail", args=[acc.pk]))
        out["paper_list"] = c.get(reverse("paper_list"))
        out["nl-ok"] = c.post(reverse("nl_strategy"), {"text": "MACD 12 26 9 auf Ethereum, 4h"}, follow=True)
        out["nl-fail"] = c.post(reverse("nl_strategy"), {"text": "Wetter morgen"}, follow=True)
        out["bad-form"] = c.post("/", {"chain": "btc", "strategy": "sma_cross", "param_a": "50", "param_b": "20"}, follow=True)
        return out

    def account_pages(self):
        c = self.client
        out = {"account": c.get(reverse("account")), "impressum": c.get(reverse("impressum")),
               "datenschutz": c.get(reverse("datenschutz")), "404": c.get("/gibt-es-nicht/")}
        c.logout()
        c.cookies["tb_lang"] = "en"
        for name in ("login", "register", "forgot", "forgot_done", "reset_done", "register_sent"):
            out["anon:" + name] = c.get(reverse(name))
        out["anon:login-bad"] = c.post(reverse("login"), {"username": "x@y.de", "password": "falsch"})
        out["anon:register-bad"] = c.post(reverse("register"), {"email": "kaputt", "password1": "a", "password2": "b"})
        return out

    def check(self, pages, label):
        problems = {}
        for name, resp in pages.items():
            html = resp.content.decode()
            if '<html lang="en"' not in html:
                problems.setdefault("<html lang>", []).append(name)
            for text in i18n_check.untranslated(html):
                problems.setdefault(text, []).append(name)
        return problems

    def test_all_pages_are_english(self):
        problems = {}
        i18n.MISSES = set()
        try:
            for part in (self.pages, self.flows, self.account_pages):
                for k, v in self.check(part(), part.__name__).items():
                    problems.setdefault(k, []).extend(v)
            misses = {m for m in i18n.MISSES if not i18n_check.is_kept(m)}
        finally:
            i18n.MISSES = None
        if DUMP:
            with open(DUMP, "w", encoding="utf-8") as f:
                for text, where in problems.items():
                    f.write(f"{text}\t[{','.join(sorted(set(where))[:3])}]\n")
                for m in sorted(misses):
                    f.write(f"{m}\t[unübersetzt]\n")
        bad = sorted(set(problems) | misses)
        self.assertFalse(bad, f"{len(bad)} Texte ohne Übersetzung, z. B.: {bad[:5]}")


class PdfLanguageTests(TestCase):
    """PDF-Export folgt der gewählten Sprache (Fußzeile und Texte)."""

    @classmethod
    def setUpTestData(cls):
        world(cls)

    def tearDown(self):
        i18n.set_lang("de")

    def _text(self, pdf: bytes) -> str:
        import io
        try:
            from pypdf import PdfReader
        except ImportError:
            self.skipTest("pypdf nicht installiert")
        return "\n".join(p.extract_text() for p in PdfReader(io.BytesIO(pdf)).pages)

    def test_pdf_english_and_german(self):
        from . import export
        run = self.runs["single"]
        i18n.set_lang("en")
        en = self._text(export.summary_pdf(run))
        self.assertIn("Not investment advice", en)
        self.assertNotIn("Keine Anlageberatung", en)
        bad = [ln for ln in en.splitlines() if i18n_check.looks_german(ln) and not i18n_check.is_kept(ln.strip())]
        self.assertLess(len(bad), 4, "deutsche Reste im PDF: " + " | ".join(bad[:6]))
        i18n.set_lang("de")
        self.assertIn("Keine Anlageberatung", self._text(export.summary_pdf(run)))
