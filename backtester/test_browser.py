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
        p.click("form button")
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
        p.click("form button")
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
        p.wait_for_selector("#price .js-plotly-plot, #price .plot-container", timeout=15000)
        self.assertTrue(p.locator("#chart .plot-container").count() >= 1)
        self.assertTrue(p.locator("#plausbox").is_visible())
        self.assertTrue(p.locator("#aibox").is_visible())
        self.assertIn("Sharpe", p.inner_text("body"))
        run = BacktestRun.objects.get(owner=self.user)
        self.assertEqual((run.status, run.source), ("done", "synthetic"))
        self.assertEqual(self.js_errors, [])

    def test_train_test_run_shows_heatmap_and_stability(self):
        self.login()
        self.run_synthetic(mode="split")
        self.page.wait_for_selector("#heatmap .plot-container", timeout=15000)
        self.assertTrue(self.page.locator("#stabbox").is_visible())
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
        a.wait_for_selector("#price .plot-container", timeout=15000)
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
        # Favorit und Tags
        p.click(".runmeta button.star")
        p.wait_for_selector(".runmeta button.star.on")
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
