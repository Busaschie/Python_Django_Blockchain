"""Browser-Tests der Oberfläche (Playwright + Chromium) gegen einen echten Testserver.

Geprüft wird, was reine Django-Tests nicht sehen: Layout (3 Spalten, Karten nebeneinander), Tooltips per Klick,
ein kompletter Lauf im Browser (Formular -> Berechnung -> Dashboard mit Diagrammen) und JavaScript-Fehler.

Ohne Playwright/Chromium werden die Tests übersprungen (`pip install playwright && playwright install chromium`).
In der CI-Pipeline ist REQUIRE_BROWSER_TESTS=1 gesetzt: dort ist ein fehlender Browser ein Fehler, kein Überspringen.
Die Berechnung läuft synchron (kein Thread-Pool), Kurse sind synthetisch, plotly.js kommt aus dem Python-Paket
statt vom CDN, damit die Tests ohne Internet laufen.
"""
import os
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase

from . import jobs
from .models import BacktestRun

os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")   # Playwright (sync) läuft neben dem ORM im selben Prozess

PASSWORD = "Sehr-gutes-Passwort-42"
REQUIRED = os.environ.get("REQUIRE_BROWSER_TESTS") == "1"


def _plotly_js() -> str:
    try:
        import plotly
        p = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except Exception:  # noqa: BLE001
        return ""


def _sync_submit(pk: int) -> None:
    run = BacktestRun.objects.get(pk=pk)
    run.status = "running"
    jobs.compute(run)
    run.save()


class BrowserCase(StaticLiveServerTestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            try:
                cls._browser = cls._pw.chromium.launch()
            except Exception:  # noqa: BLE001  (vorinstallierter Browser mit anderer Version)
                exe = os.environ.get("CHROMIUM_PATH") or "/opt/pw-browsers/chromium"
                cls._browser = cls._pw.chromium.launch(executable_path=exe)
        except Exception as exc:  # noqa: BLE001
            if getattr(cls, "_pw", None):
                cls._pw.stop()
            if REQUIRED:
                raise
            raise unittest.SkipTest(f"Playwright/Chromium nicht verfügbar: {str(exc)[:80]}")
        try:
            super().setUpClass()
        except Exception:
            cls._browser.close(); cls._pw.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._browser.close()
        cls._pw.stop()

    def setUp(self):
        self.user = get_user_model().objects.create_user("niko@example.com", "niko@example.com", PASSWORD)
        self.ctx = self._browser.new_context(viewport={"width": 1440, "height": 1000}, locale="de-DE")
        # nichts aus dem Internet laden (Schriften, Tracker ...): schneller und unabhängig vom Netz
        self.ctx.route(lambda u: not u.startswith(("http://localhost", "http://127.0.0.1", "data:")),
                       lambda r: r.abort())
        js = _plotly_js()
        if js:
            self.ctx.route("**/cdn.plot.ly/**", lambda r: r.fulfill(status=200, content_type="application/javascript", body=js))
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(15000)
        self.js_errors = []
        self.page.on("pageerror", lambda e: self.js_errors.append(str(e)))
        self.addCleanup(self.ctx.close)

    def url(self, path=""):
        return self.live_server_url + path

    def login(self):
        p = self.page
        p.goto(self.url("/anmelden/"))
        p.fill("input[name=username]", "niko@example.com")
        p.fill("input[name=password]", PASSWORD)
        p.click("form:not(.lang) button")
        p.wait_for_url(self.url("/"), wait_until="domcontentloaded")

    def run_synthetic(self, strategy="sma_cross", mode="single"):
        p = self.page
        p.goto(self.url("/"))
        p.select_option("#id_strategy", strategy)
        p.select_option("#id_mode", mode)
        p.select_option("#id_source", "synthetic")
        p.select_option("#id_timeframe", "1d")
        p.fill("#id_start_date", "2024-01-01")
        p.fill("#id_end_date", "2025-12-31")
        with mock.patch("backtester.views.jobs.submit", _sync_submit):
            p.click("button:text('Backtest starten')")
            p.wait_for_url("**/run/*/", wait_until="domcontentloaded")


class LoginAndLayoutTests(BrowserCase):
    def test_login_required_and_wrong_password(self):
        p = self.page
        p.goto(self.url("/"))
        self.assertIn("/anmelden/", p.url)
        p.fill("input[name=username]", "niko@example.com")
        p.fill("input[name=password]", "falsch")
        p.click("form:not(.lang) button")
        self.assertIn("/anmelden/", p.url)
        self.assertTrue(p.locator(".errorlist, .err").first.is_visible())

    def test_three_columns_side_by_side(self):
        self.login()
        boxes = [self.page.locator(sel).first.bounding_box() for sel in (".cols > section:nth-of-type(1)",
                                                                         ".cols > section:nth-of-type(2)", ".cols > aside")]
        self.assertTrue(all(boxes))
        xs = [b["x"] for b in boxes]
        self.assertEqual(xs, sorted(xs))
        self.assertGreater(xs[1] - xs[0], 200)
        self.assertGreater(xs[2] - xs[1], 200)
        self.assertEqual(self.js_errors, [])

    def test_narrow_screen_stacks_without_horizontal_scroll(self):
        self.login()
        self.page.set_viewport_size({"width": 420, "height": 900})
        self.assertLessEqual(self.page.evaluate("document.documentElement.scrollWidth"), 420 + 1)
        a, b = (self.page.locator(f".cols > section:nth-of-type({i})").first.bounding_box() for i in (1, 2))
        self.assertLessEqual(a["x"], b["x"] + 1)

    def test_legend_is_at_the_bottom(self):
        self.login()
        legend = self.page.locator("#legende").bounding_box()
        cols = self.page.locator(".cols").bounding_box()
        self.assertGreaterEqual(legend["y"], cols["y"] + cols["height"] - 1)


class TooltipTests(BrowserCase):
    def test_click_on_info_icon_shows_and_hides_text(self):
        self.login()
        p = self.page
        icon = p.locator("#bt-form .tip").first
        text = icon.get_attribute("data-tip")
        self.assertTrue(text)
        icon.click()
        p.wait_for_selector("#tip", state="visible")
        self.assertIn(text[:30], p.inner_text("#tip"))
        p.keyboard.press("Escape")
        p.mouse.click(5, 400)
        p.wait_for_selector("#tip", state="hidden")


class RunInBrowserTests(BrowserCase):
    def test_complete_run_shows_dashboard_with_charts(self):
        self.login()
        self.run_synthetic()
        p = self.page
        p.wait_for_selector("#price .main-svg", timeout=15000)
        self.assertTrue(p.locator("#chart  .main-svg").count() >= 1)
        self.assertTrue(p.locator("#tab-ov").is_visible())
        self.assertFalse(p.locator("#plausbox").is_visible())            # liegt im Reiter Robustheit
        p.click("#tb-rob")
        self.assertTrue(p.locator("#plausbox").is_visible())
        self.assertFalse(p.locator("#price").is_visible())
        p.click("#tb-ki")
        self.assertTrue(p.locator("#aibox").is_visible())
        p.click("#tb-ov")
        self.assertTrue(p.locator("#price").is_visible())
        p.click("#tb-kpi")
        self.assertIn("Sharpe", p.inner_text("body"))
        p.click("#tb-ov")
        run = BacktestRun.objects.get(owner=self.user)
        self.assertEqual((run.status, run.source), ("done", "synthetic"))
        self.assertEqual(self.js_errors, [])

    def test_train_test_run_shows_heatmap_and_stability(self):
        self.login()
        self.run_synthetic(mode="split")
        self.page.wait_for_selector("#heatmap  .main-svg", timeout=15000)
        self.page.click("#tb-rob")
        self.assertTrue(self.page.locator("#stabbox").is_visible())
        self.page.wait_for_selector("#stabchart  .main-svg", timeout=15000)
        self.assertGreater(self.page.locator("#stabchart .main-svg").first.bounding_box()["width"], 100)  # nach dem Einblenden richtig skaliert
        self.assertEqual(self.js_errors, [])

    def test_history_entry_opens_run_and_delete_works(self):
        self.login()
        self.run_synthetic()
        run = BacktestRun.objects.get(owner=self.user)
        self.page.goto(self.url("/"))
        self.page.click(f".hist a[href='/run/{run.pk}/']")
        self.page.wait_for_url(self.url(f"/run/{run.pk}/"))
        self.page.once("dialog", lambda d: d.accept())
        self.page.click(".hist button.del")
        self.page.wait_for_url(self.url("/"))
        self.assertFalse(BacktestRun.objects.filter(owner=self.user).exists())

    def test_csv_and_pdf_export_download(self):
        self.login()
        self.run_synthetic()
        self.page.click("#tb-kpi")
        for link, ext in (("trades.csv", ".csv"), ("auswertung.pdf", ".pdf")):
            with self.page.expect_download() as dl:
                self.page.click(f"a[href$='{link}']")
            self.assertTrue(dl.value.suggested_filename.endswith(ext))


class AccountPageTests(BrowserCase):
    def test_three_cards_side_by_side(self):
        self.login()
        self.page.goto(self.url("/konto/"))
        boxes = [b.bounding_box() for b in self.page.locator(".acc .grid > .box").all()]
        self.assertEqual(len(boxes), 3)
        self.assertEqual(len({round(b["y"]) for b in boxes}), 1)           # gleiche Zeile
        self.assertEqual([b["x"] for b in boxes], sorted(b["x"] for b in boxes))
        self.assertEqual(self.js_errors, [])


class ShareAndOrganizeTests(BrowserCase):
    def test_share_link_opens_read_only_page_without_login(self):
        self.login()
        self.run_synthetic()
        p = self.page
        p.click("button:text('Öffentlichen Link erzeugen')")
        p.wait_for_selector("#shareurl", timeout=15000)
        url = p.input_value("#shareurl")
        self.assertIn("/geteilt/", url)
        anon = self._browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(anon.close)
        js = _plotly_js()
        if js:
            anon.route("**/cdn.plot.ly/**", lambda r: r.fulfill(status=200, content_type="application/javascript", body=js))
        a = anon.new_page()
        errors = []
        a.on("pageerror", lambda e: errors.append(str(e)))
        a.goto(url.replace(self.live_server_url, self.live_server_url), wait_until="domcontentloaded")
        a.wait_for_selector("#price  .main-svg", timeout=15000)
        self.assertIn("Geteilte Auswertung", a.inner_text("#sharedbanner"))
        for sel in ("#bt-form", "#sharebox", ".hist", "#tplbox"):
            self.assertEqual(a.locator(sel).count(), 0, sel)
        self.assertEqual(errors, [])
        # Freigabe beenden: der Link ist danach tot
        p.click("button:text('Freigabe beenden')")
        p.wait_for_selector("button:text('Öffentlichen Link erzeugen')")
        self.assertEqual(a.goto(url).status, 404)

    def test_template_favorite_tag_filter_and_report(self):
        self.login()
        self.run_synthetic()
        p = self.page
        p.click("#tb-kpi")
        # Favorit und Tags
        p.click(".runmeta button.star")
        p.wait_for_selector(".runmeta button.star.on", state="attached")
        p.wait_for_load_state("load")
        p.click("#tb-kpi")                      # nach dem Neuladen startet wieder der Reiter Übersicht
        p.fill(".tagform input[name=tags]", "btc, Test")
        p.click(".tagform button:text('Tags speichern')")
        p.wait_for_selector(".hist a.tag:text('Test')")
        p.select_option(".histfilter select[name=tag]", "Test")
        p.click(".histfilter button:text('Filtern')")
        p.wait_for_selector(".hist li.cur")
        self.assertIn("tag=Test", p.url)
        p.goto(self.url("/?tag=gibtesnicht"))
        self.assertIn("Keine Treffer", p.inner_text(".hist"))
        # Vorlage speichern (Enter im Namensfeld darf keinen Backtest starten) und wieder laden
        p.goto(self.url("/"))
        p.select_option("#id_strategy", "rsi")
        p.fill("#id_fee", "0.002")
        p.fill("#template_name", "Mein RSI")
        p.press("#template_name", "Enter")
        p.wait_for_selector("#tplbox ul.tpl a:text('Mein RSI')")
        self.assertEqual(BacktestRun.objects.filter(owner=self.user).count(), 1)     # kein zweiter Lauf
        p.goto(self.url("/"))
        p.click("#tplbox a:text('Mein RSI')")
        p.wait_for_url("**vorlage=*", wait_until="domcontentloaded")
        self.assertEqual(p.input_value("#id_strategy"), "rsi")
        self.assertEqual(float(p.input_value("#id_fee")), 0.002)
        # Bericht aus Auswahl
        p.goto(self.url("/"))
        p.check(".hist input.rsel")
        with p.expect_download() as dl:
            p.click("#reportform button:text('Bericht aus Auswahl')")
        self.assertTrue(dl.value.suggested_filename.endswith(".pdf"))
        self.assertEqual(self.js_errors, [])


class NewStrategyBrowserTests(BrowserCase):
    def test_switching_strategy_sets_defaults_labels_and_param_c(self):
        self.login()
        p = self.page
        p.goto(self.url("/"))
        p.select_option("#id_mode", "single")
        for strat, vals, c_visible in (("macd", ("12", "26", "9"), True), ("donchian", ("20", "10", None), False),
                                       ("bollinger", ("20", "20", None), False), ("momentum", ("30", "0", None), False),
                                       ("rsi", ("14", "30", "70"), True)):
            p.select_option("#id_strategy", strat)
            self.assertEqual((p.input_value("#id_param_a"), p.input_value("#id_param_b")), vals[:2], strat)
            self.assertEqual(p.locator("[data-field=param_c]").is_visible(), bool(c_visible), strat)
            if vals[2]:
                self.assertEqual(p.input_value("#id_param_c"), vals[2], strat)
        p.select_option("#id_strategy", "donchian")
        self.assertIn("Einstieg", p.inner_text("[data-field=param_a] label"))
        self.assertEqual(self.js_errors, [])

    def test_each_new_strategy_runs_single_and_split(self):
        self.login()
        for strat in ("bollinger", "macd", "donchian", "momentum"):
            self.run_synthetic(strategy=strat)
            self.page.wait_for_selector("#chart  .main-svg", timeout=15000)
            run = BacktestRun.objects.filter(owner=self.user).latest("pk")
            self.assertEqual((run.strategy, run.status), (strat, "done"), strat)
            self.assertIn("Robustheit", self.page.inner_text("body"))
        self.run_synthetic(strategy="momentum", mode="split")
        self.page.wait_for_selector("#heatmap  .main-svg", timeout=15000)
        self.assertEqual(self.js_errors, [])


class AiBoxesBrowserTests(BrowserCase):
    def test_strategy_in_words_prefills_form_without_starting(self):
        self.login()
        p = self.page
        p.goto(self.url("/"))
        p.fill("#nlbox textarea", "MACD 12 26 9 auf Ethereum, 4h")
        p.click("#nlbox button:text('In Einstellungen übersetzen')")
        p.wait_for_url("**/?plan=1*", wait_until="domcontentloaded")
        self.assertIn("Verstanden als: MACD", p.inner_text("#nlbox"))
        self.assertEqual(p.input_value("#id_strategy"), "macd")
        self.assertEqual((p.input_value("#id_param_a"), p.input_value("#id_param_b"), p.input_value("#id_param_c")), ("12", "26", "9"))
        self.assertEqual(p.input_value("#id_timeframe"), "4h")
        self.assertTrue(p.locator("#chain-eth").is_checked())
        self.assertEqual(BacktestRun.objects.filter(owner=self.user).count(), 0)         # nichts gestartet
        p.fill("#nlbox textarea", "Wie wird das Wetter?")
        p.click("#nlbox button:text('In Einstellungen übersetzen')")
        p.wait_for_selector("#nlbox .err")
        self.assertIn("keine Strategie erkannt", p.inner_text("#nlbox"))
        self.assertEqual(self.js_errors, [])

    def test_fixed_questions_have_no_free_text_and_answer_appears(self):
        self.login()
        self.run_synthetic()
        p = self.page
        p.click("#tb-ki")
        p.wait_for_selector("#askbox")
        self.assertEqual(p.locator("#askbox textarea, #askbox input[type=text]").count(), 0)
        self.assertFalse(p.locator("#ask_period").is_visible())
        p.select_option("#ask_q", "period")
        self.assertTrue(p.locator("#ask_period").is_visible())
        p.select_option("#ask_q", "costs")
        p.click("#askbox button:text('Fragen')")
        p.wait_for_selector("#askbox .askitem")
        self.assertIn("Kosten", p.inner_text("#askbox .askitem"))
        self.assertEqual(self.js_errors, [])


class ThemeBrowserTests(BrowserCase):
    def test_theme_toggle_persists_and_default_is_dark(self):
        self.login()
        p = self.page
        p.goto(self.url("/"))
        self.assertIsNone(p.evaluate("document.documentElement.getAttribute('data-theme')"))      # Standard: dunkel
        bg_dark = p.evaluate("getComputedStyle(document.body).backgroundColor")
        p.click("#themebtn")
        self.assertEqual(p.evaluate("document.documentElement.getAttribute('data-theme')"), "light")
        self.assertNotEqual(p.evaluate("getComputedStyle(document.body).backgroundColor"), bg_dark)
        p.reload()
        self.assertEqual(p.evaluate("document.documentElement.getAttribute('data-theme')"), "light")  # bleibt nach Neuladen
        p.click("#themebtn")
        self.assertIsNone(p.evaluate("document.documentElement.getAttribute('data-theme')"))
        self.assertEqual(self.js_errors, [])

    def test_charts_follow_theme(self):
        self.login()
        self.run_synthetic()
        p = self.page
        p.wait_for_selector("#chart  .main-svg", timeout=15000)
        dark_font = p.evaluate("document.querySelector('#chart .gtitle, #chart .xtick text').style.fill || getComputedStyle(document.querySelector('#chart .xtick text')).fill")
        p.click("#themebtn")
        p.wait_for_timeout(500)
        light_font = p.evaluate("getComputedStyle(document.querySelector('#chart .xtick text')).fill")
        self.assertNotEqual(dark_font, light_font)
        self.assertEqual(self.js_errors, [])

    def test_disclaimer_visible_on_dashboard_and_legal_pages(self):
        self.login()
        p = self.page
        for path in ("/", "/impressum/", "/datenschutz/"):
            p.goto(self.url(path))
            self.assertTrue(p.locator("#disclaimer").is_visible(), path)
        self.assertIn("Keine Anlageberatung", p.inner_text("#disclaimer"))
