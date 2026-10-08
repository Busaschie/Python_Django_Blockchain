"""Tests: Teilen (öffentlicher Nur-Lesen-Link), PDF-Bericht, Favoriten/Tags/Filter und Vorlagen."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import export
from .models import BacktestRun, RunTemplate
from .test_extras import computed_run
from .views import MAX_TAGS, MAX_TAG_LEN, MAX_TEMPLATES, parse_tags

User = get_user_model()
PW = "Sehr-gutes-Pw-17"


def form_post(**over):
    """Gültige Formularwerte des Dashboards (Einzellauf, synthetische Daten)."""
    d = dict(chain="btc", strategy="sma_cross", mode="single", train_frac=70, wf_folds=5, wf_train_mult=3,
             param_a=20, param_b=50, param_c=70, combo_logic="trend", rsi_period=14, rsi_entry=40, rsi_exit=70,
             timeframe="1d", start_date="2024-01-01", end_date="2024-12-31", fee=0.001, slippage=0.0005,
             stop_loss=5, take_profit="", trailing_stop="", size_mode="full", size_value=50, execution="open",
             source="synthetic", exchange="binance")
    d.update(over)
    return d


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("me@x.de", "me@x.de", PW)
        cls.other = User.objects.create_user("other@x.de", "other@x.de", PW)
        cls.r1, _ = computed_run("single", "sma_cross", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)


class ShareTests(Base):
    def share(self, action="on", run=None):
        r = run or self.r1
        self.client.post(reverse("share_toggle", args=[r.pk]), {"action": action})
        r.refresh_from_db()
        return r.share_token

    def test_on_off_and_renew(self):
        self.assertEqual(self.r1.share_token, "")
        token = self.share("on")
        self.assertGreaterEqual(len(token), 20)
        self.assertEqual(self.share("on"), token)                     # erneutes "an" ändert den Link nicht
        new = self.share("renew")
        self.assertNotEqual(new, token)
        self.assertEqual(self.client.get(reverse("shared", args=[token])).status_code, 404)   # alter Link tot
        self.assertEqual(self.share("off"), "")
        self.assertEqual(self.client.get(reverse("shared", args=[new])).status_code, 404)

    def test_public_page_is_read_only_and_private(self):
        token = self.share("on")
        self.client.logout()
        r = self.client.get(reverse("shared", args=[token]))
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        self.assertIn("Geteilte Auswertung", html)
        self.assertIn("Sharpe Ratio", html)
        self.assertIn('id="plausbox"', html)
        for hidden in ('id="bt-form"', 'name="csrfmiddlewaretoken"', "me@x.de", 'id="tplbox"', 'id="sharebox"', 'class="box hist"',
                       "Gelaufene Auswertungen", 'id="sigbox"', reverse("delete", args=[self.r1.pk])):
            self.assertNotIn(hidden, html, hidden)
        self.assertIn("noindex", r["X-Robots-Tag"])
        self.assertEqual(r["Referrer-Policy"], "no-referrer")
        self.assertIn(reverse("shared_pdf", args=[token]), html)

    def test_public_downloads(self):
        token = self.share("on")
        self.client.logout()
        csv = self.client.get(reverse("shared_trades", args=[token]))
        self.assertEqual(csv.status_code, 200)
        self.assertTrue(csv.content.startswith(b"\xef\xbb\xbf"))
        pdf = self.client.get(reverse("shared_pdf", args=[token]))
        self.assertEqual((pdf.status_code, pdf["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_wrong_short_or_unfinished_tokens_are_404(self):
        for tok in ("x" * 24, "short", "a" * 5):
            self.assertEqual(self.client.get(reverse("shared", args=[tok])).status_code, 404, tok)
        token = self.share("on")
        BacktestRun.objects.filter(pk=self.r1.pk).update(status="running")
        self.client.logout()
        self.assertEqual(self.client.get(reverse("shared", args=[token])).status_code, 404)

    def test_only_owner_can_change_sharing(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("share_toggle", args=[self.r1.pk]), {"action": "on"}).status_code, 404)
        self.r1.refresh_from_db()
        self.assertEqual(self.r1.share_token, "")
        self.assertEqual(self.client.get(reverse("share_toggle", args=[self.r1.pk])).status_code, 405)

    def test_owner_page_shows_link_only_when_shared(self):
        html = self.client.get(reverse("detail", args=[self.r1.pk])).content.decode()
        self.assertIn("Öffentlichen Link erzeugen", html)
        token = self.share("on")
        html = self.client.get(reverse("detail", args=[self.r1.pk])).content.decode()
        self.assertIn(reverse("shared", args=[token]), html)
        self.assertIn("Freigabe beenden", html)

    def test_deleting_run_or_account_kills_link(self):
        token = self.share("on")
        self.client.post(reverse("delete", args=[self.r1.pk]))
        self.client.logout()
        self.assertEqual(self.client.get(reverse("shared", args=[token])).status_code, 404)


class ReportTests(Base):
    def setUp(self):
        super().setUp()
        self.run2, _ = computed_run("split", "rsi", owner=self.user)
        self.run3, _ = computed_run("walkforward", "combo", owner=self.user)

    def test_report_of_selected_runs(self):
        url = reverse("export_report") + f"?run={self.r1.pk}&run={self.run2.pk}&run={self.run3.pk}"
        r = self.client.get(url)
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "application/pdf"))
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertIn("3_laeufe", r["Content-Disposition"])
        single = self.client.get(reverse("export_pdf", args=[self.r1.pk])).content
        self.assertGreater(len(r.content), len(single))

    def test_report_for_batch(self):
        BacktestRun.objects.filter(pk__in=[self.r1.pk, self.run2.pk]).update(batch="abc123")
        r = self.client.get(reverse("export_report") + "?batch=abc123")
        self.assertEqual(r.status_code, 200)
        self.assertIn("2_laeufe", r["Content-Disposition"])

    def test_foreign_unfinished_or_missing_runs_are_ignored(self):
        foreign, _ = computed_run("single", "rsi", owner=self.other)
        BacktestRun.objects.filter(pk=self.run3.pk).update(status="error")
        r = self.client.get(reverse("export_report") + f"?run={foreign.pk}&run={self.run3.pk}&run=abc&run=99999")
        self.assertEqual(r.status_code, 302)                       # nichts Brauchbares ausgewählt
        r = self.client.get(reverse("export_report") + f"?run={foreign.pk}&run={self.r1.pk}")
        self.assertIn("1_laeufe", r["Content-Disposition"])

    def test_report_is_limited(self):
        ids = "&".join(f"run={self.r1.pk}" for _ in range(3)) + "&" + "&".join(f"run={i}" for i in range(500, 520))
        self.assertEqual(self.client.get(reverse("export_report") + "?" + ids).status_code, 200)
        self.assertEqual(len(export.report_pdf([self.r1] * (export.MAX_REPORT_RUNS + 3))) > 1000, True)

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("export_report") + f"?run={self.r1.pk}").status_code, 302)

    def test_selection_ui_present(self):
        html = self.client.get(reverse("index")).content.decode()
        self.assertIn(f'name="run" value="{self.r1.pk}" form="reportform"', html)
        self.assertIn("Bericht aus Auswahl (PDF)", html)


class TagParsingTests(TestCase):
    def test_parse_tags(self):
        self.assertEqual(parse_tags(""), "")
        self.assertEqual(parse_tags(" btc , Test ,TEST,, x "), "|btc|Test|x|")
        self.assertEqual(parse_tags("a|b"), "|a|b|")                               # Trennzeichen kann nicht eingeschleust werden
        self.assertEqual(parse_tags("a" * 100), "|" + "a" * MAX_TAG_LEN + "|")
        self.assertEqual(len([t for t in parse_tags(",".join(f"t{i}" for i in range(20))).split("|") if t]), MAX_TAGS)


class FavoriteTagFilterTests(Base):
    def setUp(self):
        super().setUp()
        self.run2, _ = computed_run("single", "rsi", owner=self.user)
        self.run3, _ = computed_run("single", "combo", owner=self.user)

    def page(self, query=""):
        return self.client.get(reverse("index") + query).content.decode()

    def test_favorite_toggle_and_redirect(self):
        r = self.client.post(reverse("favorite_toggle", args=[self.r1.pk]), {"next": "/?fav=1"})
        self.assertRedirects(r, "/?fav=1", fetch_redirect_response=False)
        self.r1.refresh_from_db()
        self.assertTrue(self.r1.favorite)
        self.client.post(reverse("favorite_toggle", args=[self.r1.pk]))
        self.r1.refresh_from_db()
        self.assertFalse(self.r1.favorite)

    def test_open_redirect_is_blocked(self):
        r = self.client.post(reverse("favorite_toggle", args=[self.r1.pk]), {"next": "https://evil.example/x"})
        self.assertRedirects(r, reverse("detail", args=[self.r1.pk]), fetch_redirect_response=False)

    def test_tags_set_and_show(self):
        self.client.post(reverse("tags_set", args=[self.r1.pk]), {"tags": "BTC, konservativ"})
        self.r1.refresh_from_db()
        self.assertEqual((self.r1.tags, self.r1.tag_list), ("|BTC|konservativ|", ["BTC", "konservativ"]))
        self.assertIn('href="/?tag=konservativ"', self.page())
        html = self.client.get(reverse("detail", args=[self.r1.pk])).content.decode()
        self.assertIn('value="BTC, konservativ"', html)

    def test_filter_by_tag_and_favorite(self):
        BacktestRun.objects.filter(pk=self.r1.pk).update(tags="|alpha|", favorite=True)
        BacktestRun.objects.filter(pk=self.run2.pk).update(tags="|alpha|beta|")
        link = lambda r: reverse("detail", args=[r.pk])   # noqa: E731
        both = self.page("?tag=alpha")
        self.assertTrue(link(self.r1) in both and link(self.run2) in both and f'href="{link(self.run3)}"' not in both)
        only_beta = self.page("?tag=beta")
        self.assertTrue(f'href="{link(self.run2)}"' in only_beta and f'href="{link(self.r1)}"' not in only_beta)
        fav = self.page("?fav=1")
        self.assertTrue(f'href="{link(self.r1)}"' in fav and f'href="{link(self.run2)}"' not in fav)
        both_filters = self.page("?tag=beta&fav=1")
        self.assertIn("Keine Treffer", both_filters)
        self.assertIn("Keine Treffer", self.page("?tag=alp"))

    def test_filter_options_list_only_own_tags(self):
        BacktestRun.objects.filter(pk=self.r1.pk).update(tags="|mine|")
        other_run, _ = computed_run("single", "rsi", owner=self.other)
        BacktestRun.objects.filter(pk=other_run.pk).update(tags="|secret|")
        html = self.page()
        self.assertIn('<option value="mine"', html)
        self.assertNotIn("secret", html)

    def test_only_own_runs(self):
        self.client.force_login(self.other)
        self.assertEqual(self.client.post(reverse("favorite_toggle", args=[self.r1.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("tags_set", args=[self.r1.pk]), {"tags": "x"}).status_code, 404)
        self.r1.refresh_from_db()
        self.assertEqual((self.r1.favorite, self.r1.tags), (False, ""))


class TemplateTests(Base):
    def save(self, name="Mein Setup", **over):
        return self.client.post(reverse("index"), {**form_post(**over), "template_name": name, "save_template": "1"})

    def test_save_creates_template_but_no_run(self):
        before = BacktestRun.objects.count()
        r = self.save("Konservativ", strategy="rsi", stop_loss=3, fee=0.002)
        tpl = RunTemplate.objects.get(owner=self.user, name="Konservativ")
        self.assertRedirects(r, f"/?vorlage={tpl.pk}", fetch_redirect_response=False)
        self.assertEqual(BacktestRun.objects.count(), before)
        j = tpl.job
        self.assertEqual((j["strategy"], j["stop_loss"], j["fee"], j["period_days"]), ("rsi", 3, 0.002, 366))
        self.assertNotIn("start_date", j)
        self.assertNotIn("end_date", j)

    def test_load_template_prefills_form_with_period_up_to_today(self):
        self.save("Lang", strategy="combo", mode="walkforward", start_date="2023-01-01", end_date="2023-12-31")
        tpl = RunTemplate.objects.get(name="Lang")
        html = self.client.get(f"/?vorlage={tpl.pk}").content.decode()
        today = timezone.localdate()
        self.assertIn(f'value="{today:%Y-%m-%d}"', html)
        self.assertIn(f'value="{today - timedelta(days=364):%Y-%m-%d}"', html)          # 365 Tage bis heute
        self.assertRegex(html, r'<option value="combo" selected>')
        self.assertRegex(html, r'<option value="walkforward" selected>')

    def test_same_name_overwrites_and_limit(self):
        self.save("A", fee=0.001)
        self.save("A", fee=0.005)
        self.assertEqual(RunTemplate.objects.filter(owner=self.user).count(), 1)
        self.assertEqual(RunTemplate.objects.get(name="A").job["fee"], 0.005)
        for i in range(MAX_TEMPLATES - 1):
            RunTemplate.objects.create(owner=self.user, name=f"T{i}", job={})
        self.save("Zu viel")
        self.assertFalse(RunTemplate.objects.filter(name="Zu viel").exists())
        self.save("A", fee=0.009)                                       # Überschreiben bleibt trotz Limit möglich
        self.assertEqual(RunTemplate.objects.get(name="A").job["fee"], 0.009)

    def test_name_required_and_invalid_form_not_saved(self):
        self.save("   ")
        self.assertFalse(RunTemplate.objects.exists())
        self.save("X", fee=-1)
        self.assertFalse(RunTemplate.objects.exists())

    def test_templates_are_private(self):
        mine = RunTemplate.objects.create(owner=self.user, name="privat", job={"strategy": "rsi"})
        self.client.force_login(self.other)
        self.assertEqual(self.client.get(f"/?vorlage={mine.pk}").status_code, 404)
        self.assertNotIn("privat", self.client.get("/").content.decode())
        self.assertEqual(self.client.post(reverse("template_delete", args=[mine.pk])).status_code, 404)
        self.assertTrue(RunTemplate.objects.filter(pk=mine.pk).exists())
        self.save("gleicher Name ok")
        self.assertEqual(RunTemplate.objects.filter(owner=self.other).count(), 1)

    def test_delete_template(self):
        t = RunTemplate.objects.create(owner=self.user, name="weg", job={})
        self.assertEqual(self.client.get(reverse("template_delete", args=[t.pk])).status_code, 405)
        self.client.post(reverse("template_delete", args=[t.pk]))
        self.assertFalse(RunTemplate.objects.filter(pk=t.pk).exists())

    def test_start_button_without_save_still_creates_run(self):
        from unittest import mock
        with mock.patch("backtester.views.jobs.submit"):
            r = self.client.post(reverse("index"), {**form_post(), "template_name": "ignoriert"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(BacktestRun.objects.filter(owner=self.user, status="queued").count(), 1)
        self.assertFalse(RunTemplate.objects.exists())
