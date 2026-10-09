"""Tests: Demo-Zugang (Link, Konten, Grenzen, Sperren, Aufräumen, Englisch)."""
import io
import os
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from accounts import demo, throttle
from accounts.models import Attempt
from backtester.models import AiCall, BacktestRun
from backtester.test_extras import computed_run
from backtester.test_organize import form_post
from config import i18n_check

User = get_user_model()
PW = "Sehr-gutes-Pw-17"


def link(token=None):
    return reverse("demo_login", args=[token or demo.make_token()])


def demo_user():
    return User.objects.get(username__endswith="@" + demo.DOMAIN)


class LinkTests(TestCase):
    def test_valid_link_creates_own_limited_account_and_logs_in(self):
        r = self.client.get(link())
        self.assertRedirects(r, reverse("index"), fetch_redirect_response=False)
        u = demo_user()
        self.assertTrue(demo.is_demo(u))
        self.assertFalse(u.has_usable_password())
        self.assertEqual(u.email, "")
        self.assertFalse(u.is_staff)
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, 'id="demobar"')
        self.assertContains(page, "Läufe: 0 von 20")
        self.assertContains(page, "KI-Aufrufe übrig: 5 von 5")
        self.assertEqual(self.client.session.get_expiry_age(), demo.ACCOUNT_HOURS * 3600)

    def test_tampered_foreign_and_expired_tokens_are_rejected(self):
        tok = demo.make_token()
        for bad in (tok[:-3] + "xyz", "quatsch", tok + "a"):
            self.assertEqual(self.client.get(link(bad)).status_code, 403)
        from django.core import signing
        foreign = signing.dumps({"n": "x"}, salt="anderer-zweck")
        self.assertEqual(self.client.get(link(foreign)).status_code, 403)
        with mock.patch.object(demo, "TOKEN_MAX_AGE", -1):            # abgelaufen
            self.assertEqual(self.client.get(link(tok)).status_code, 403)
        self.assertEqual(User.objects.count(), 0)

    def test_token_is_valid_for_two_days(self):
        self.assertEqual(demo.TOKEN_MAX_AGE, 2 * 24 * 3600)
        tok = demo.make_token()
        with mock.patch("django.core.signing.time.time", return_value=__import__("time").time() + 2 * 24 * 3600 - 60):
            self.assertTrue(demo.valid_token(tok))
        with mock.patch("django.core.signing.time.time", return_value=__import__("time").time() + 2 * 24 * 3600 + 60):
            self.assertFalse(demo.valid_token(tok))

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"DEMO_ENABLED": "0"}):
            self.assertEqual(self.client.get(link()).status_code, 503)
        self.assertEqual(User.objects.count(), 0)

    def test_visiting_again_reuses_the_account(self):
        self.client.get(link())
        self.client.get(link())
        self.assertEqual(User.objects.count(), 1)

    def test_real_user_is_not_turned_into_demo(self):
        User.objects.create_user("me@x.de", "me@x.de", PW)
        self.client.login(username="me@x.de", password=PW)
        r = self.client.get(link())
        self.assertEqual(r.status_code, 302)
        self.assertEqual(User.objects.count(), 1)

    def test_demo_name_alone_does_not_make_a_demo_account(self):
        u = User.objects.create_user("demo-abc@demo.invalid", "", PW)      # mit Passwort: kein Demo-Konto
        self.assertFalse(demo.is_demo(u))
        s = User.objects.create_superuser("demo-adm@demo.invalid", "", PW)
        self.assertFalse(demo.is_demo(s))
        self.assertFalse(demo.is_demo(None))


class AccountLimitTests(TestCase):
    def open(self, ip="10.0.0.1"):
        c = Client()
        return c, c.get(link(), REMOTE_ADDR=ip)

    def test_at_most_five_at_once_and_logout_frees_a_slot(self):
        clients = []
        for i in range(demo.MAX_ACCOUNTS):
            c, r = self.open(f"10.0.0.{i}")
            self.assertEqual(r.status_code, 302)
            clients.append(c)
        c6, r = self.open("10.0.0.99")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(r["Retry-After"], "600")
        self.assertContains(r, "alle Demo-Plätze belegt", status_code=503)
        clients[0].post(reverse("logout"))
        self.assertEqual(User.objects.count(), demo.MAX_ACCOUNTS - 1)      # Konto beim Abmelden gelöscht
        c7, r = self.open("10.0.0.98")
        self.assertEqual(r.status_code, 302)

    def test_logout_deletes_account_and_runs(self):
        c, _ = self.open()
        u = demo_user()
        computed_run("single", "sma_cross", owner=u)
        c.post(reverse("logout"))
        self.assertFalse(User.objects.exists())
        self.assertFalse(BacktestRun.objects.exists())

    def test_expired_account_is_logged_out_and_removed_by_cleanup(self):
        c, _ = self.open()
        u = demo_user()
        User.objects.filter(pk=u.pk).update(date_joined=timezone.now() - timedelta(hours=demo.ACCOUNT_HOURS + 1))
        r = c.get("/")
        self.assertEqual(r.status_code, 302)                                # abgemeldet -> Anmeldung
        self.assertIn("/anmelden/", r["Location"])
        self.assertFalse(User.objects.exists())                             # beim Abmelden gelöscht

    def test_cleanup_removes_only_expired_demo_accounts(self):
        old = demo.create_user()
        User.objects.filter(pk=old.pk).update(date_joined=timezone.now() - timedelta(hours=demo.ACCOUNT_HOURS + 1))
        fresh = demo.create_user()
        real = User.objects.create_user("me@x.de", "me@x.de", PW)
        User.objects.filter(pk=real.pk).update(date_joined=timezone.now() - timedelta(days=100))
        out = io.StringIO()
        call_command("demo_cleanup", stdout=out)
        self.assertIn("1 abgelaufene", out.getvalue())
        self.assertEqual(set(User.objects.values_list("pk", flat=True)), {fresh.pk, real.pk})

    def test_expired_accounts_do_not_count_against_the_limit(self):
        for _ in range(demo.MAX_ACCOUNTS):
            u = demo.create_user()
            User.objects.filter(pk=u.pk).update(date_joined=timezone.now() - timedelta(hours=demo.ACCOUNT_HOURS + 1))
        c, r = self.open()
        self.assertEqual(r.status_code, 302)
        self.assertEqual(User.objects.count(), 1)

    def test_ip_throttle(self):
        for i in range(10):
            c = Client()
            self.assertEqual(c.get(link(), REMOTE_ADDR="9.9.9.9").status_code, 302)
            c.post(reverse("logout"))
        r = Client().get(link(), REMOTE_ADDR="9.9.9.9")
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r["Retry-After"], "3600")
        self.assertEqual(Client().get(link(), REMOTE_ADDR="9.9.9.8").status_code, 302)      # andere IP geht
        self.assertEqual(Attempt.objects.filter(scope="demo").count(), 11)


class RunLimitTests(TestCase):
    def setUp(self):
        self.client.get(link())
        self.user = demo_user()

    def fill(self, n):
        BacktestRun.objects.bulk_create([BacktestRun(owner=self.user, chain="btc", days=30, status="done") for _ in range(n)])

    def post(self, **extra):
        with mock.patch("backtester.views.jobs.submit"):
            return self.client.post(reverse("index"), {**form_post(), **extra})

    def test_twentieth_run_is_allowed_twenty_first_is_not(self):
        self.fill(19)
        self.assertEqual(self.post().status_code, 302)
        self.assertEqual(BacktestRun.objects.filter(owner=self.user).count(), 20)
        r = self.post()
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "höchstens 20 Läufe")
        self.assertEqual(BacktestRun.objects.filter(owner=self.user).count(), 20)

    def test_compare_creating_too_many_runs_creates_none(self):
        self.fill(15)
        r = self.post(compare_strategies="1")        # würde 7 Läufe anlegen
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "höchstens 20 Läufe")
        self.assertEqual(BacktestRun.objects.filter(owner=self.user).count(), 15)
        self.assertEqual(self.post(compare="1").status_code, 302)       # 3 Läufe passen noch

    def test_deleting_frees_room(self):
        self.fill(20)
        self.assertEqual(self.post().status_code, 200)
        BacktestRun.objects.filter(owner=self.user).first().delete()
        self.assertEqual(self.post().status_code, 302)

    def test_normal_users_have_no_limit(self):
        u = User.objects.create_user("me@x.de", "me@x.de", PW)
        BacktestRun.objects.bulk_create([BacktestRun(owner=u, chain="btc", days=30, status="done") for _ in range(25)])
        c = Client()
        c.force_login(u)
        with mock.patch("backtester.views.jobs.submit"):
            self.assertEqual(c.post(reverse("index"), form_post()).status_code, 302)


@mock.patch.dict(os.environ, {"GROQ_API_KEY": "test-key"})
class AiLimitTests(TestCase):
    def setUp(self):
        self.client.get(link())
        self.user = demo_user()
        self.run, _ = computed_run("single", "sma_cross", owner=self.user)

    def calls(self, n):
        AiCall.objects.bulk_create([AiCall(user=self.user) for _ in range(n)])

    def test_five_calls_in_total_then_rules_with_note(self):
        from backtester import ai
        self.assertEqual(ai.remaining_today(self.user), 5)
        self.calls(4)
        self.assertEqual(ai.remaining_today(self.user), 1)
        self.calls(1)
        self.assertEqual(ai.remaining_today(self.user), 0)
        self.assertEqual(ai.user_limit(self.user), 5)
        with mock.patch.object(ai, "ask_llm", side_effect=AssertionError("kein KI-Aufruf mehr")):
            ai.generate(self.run, self.user)
        self.run.refresh_from_db()
        self.assertEqual(self.run.ai_source, "regeln")
        self.assertIn("Demo-Limit für KI-Kommentare erreicht (5 je Demo-Konto)", self.run.ai_comment["note"])

    def test_limit_counts_all_days(self):
        from backtester import ai
        self.calls(5)
        AiCall.objects.filter(user=self.user).update(created_at=timezone.now() - timedelta(days=3))
        self.assertEqual(ai.remaining_today(self.user), 0)           # nicht je Tag

    def test_hint_on_the_page(self):
        self.calls(2)
        page = self.client.get(reverse("detail", args=[self.run.pk]))
        self.assertContains(page, "Demo: noch 3 von 5 KI-Aufrufen im Demo-Konto")
        self.assertContains(page, "KI-Aufrufe übrig: 3 von 5")

    def test_normal_users_keep_daily_limit(self):
        from backtester import ai
        u = User.objects.create_user("me@x.de", "me@x.de", PW)
        self.assertEqual(ai.user_limit(u), ai.limits()[0])
        AiCall.objects.bulk_create([AiCall(user=u) for _ in range(6)])
        self.assertEqual(ai.remaining_today(u), ai.limits()[0] - 6)


class BlockedActionTests(TestCase):
    def setUp(self):
        self.client.get(link())
        self.user = demo_user()
        self.run, _ = computed_run("single", "sma_cross", owner=self.user)

    def test_account_page_explains_instead_of_forms(self):
        r = self.client.get(reverse("account"))
        self.assertContains(r, 'id="demoinfo"')
        self.assertNotContains(r, "Konto endgültig löschen")
        self.assertNotContains(r, "Bestätigungslink senden")
        self.assertContains(r, "20 Läufe und 5 KI-Aufrufe")

    def test_password_email_delete_are_blocked(self):
        for name, data in (("account", {"old_password": "x", "new_password1": "a", "new_password2": "a"}),
                           ("email_change", {"new_email": "neu@x.de", "password": "x"}),
                           ("account_delete", {"password": "x", "confirm": "on"})):
            r = self.client.post(reverse(name), data)
            self.assertIn(r.status_code, (200, 302), name)
            self.assertTrue(User.objects.filter(pk=self.user.pk).exists(), name)
        r = self.client.post(reverse("email_change"), {"new_email": "neu@x.de"}, follow=True)
        self.assertContains(r, "Im Demo-Konto nicht möglich")
        self.user.refresh_from_db()
        self.assertTrue(self.user.username.endswith("@demo.invalid"))

    def test_sharing_is_blocked_with_explanation(self):
        r = self.client.post(reverse("share_toggle", args=[self.run.pk]), {"action": "on"}, follow=True)
        self.assertContains(r, "Im Demo-Konto lassen sich Auswertungen nicht teilen")
        self.run.refresh_from_db()
        self.assertEqual(self.run.share_token, "")
        page = self.client.get(reverse("detail", args=[self.run.pk]))
        self.assertContains(page, "Demo: Teilen ist im Demo-Konto gesperrt")

    def test_signal_mail_and_paper_visible_but_blocked(self):
        self.run.source = "ccxt"
        self.run.save()
        with override_settings(SIGNALS_ENABLED=False):
            page = self.client.get(reverse("detail", args=[self.run.pk]))
            self.assertContains(page, 'id="sigbox"')
            self.assertContains(page, 'id="paperbox"')
            self.assertContains(page, "Demo: Im Demo-Konto werden keine Mails versendet")
            self.assertContains(page, "Demo: Paper-Trading lässt sich im Demo-Konto nicht starten")
            r = self.client.post(reverse("signal_toggle", args=[self.run.pk]), follow=True)
            self.assertContains(r, "keine Signal-Mails versendet")
            self.run.refresh_from_db()
            self.assertFalse(self.run.signal_alert)
            r = self.client.post(reverse("paper_start", args=[self.run.pk]), {"capital": "5000"}, follow=True)
            self.assertContains(r, "Paper-Trading nicht starten")
            from backtester.models import PaperAccount
            self.assertFalse(PaperAccount.objects.exists())
            lst = self.client.get(reverse("paper_list"))
            self.assertContains(lst, "Demo: Im Demo-Konto lässt sich Paper-Trading nicht starten")

    def test_normal_users_do_not_see_signal_pages_when_disabled(self):
        u = User.objects.create_user("me@x.de", "me@x.de", PW)
        c = Client()
        c.force_login(u)
        with override_settings(SIGNALS_ENABLED=False):
            self.assertEqual(c.get(reverse("paper_list")).status_code, 404)

    def test_export_of_own_data_still_works(self):
        self.assertEqual(self.client.post(reverse("account_export")).status_code, 200)


class AdminLinkTests(TestCase):
    def test_staff_gets_a_working_link(self):
        User.objects.create_superuser("admin@x.de", "admin@x.de", PW)
        self.client.login(username="admin@x.de", password=PW)
        r = self.client.get(reverse("demo_link"))
        self.assertContains(r, "/demo/")
        self.assertContains(r, "48 Stunden gültig")
        import re
        url = re.search(r'value="https?://[^/]+(/demo/[^"]+)"', r.content.decode()).group(1)
        self.assertTrue(demo.valid_token(url.split("/")[2]))

    def test_others_get_404_or_login(self):
        self.assertEqual(self.client.get(reverse("demo_link")).status_code, 302)       # nicht angemeldet
        User.objects.create_user("me@x.de", "me@x.de", PW)
        self.client.login(username="me@x.de", password=PW)
        self.assertEqual(self.client.get(reverse("demo_link")).status_code, 404)

    def test_demo_account_cannot_open_the_link_page(self):
        self.client.get(link())
        self.assertEqual(self.client.get(reverse("demo_link")).status_code, 404)

    def test_command_prints_valid_link(self):
        out = io.StringIO()
        call_command("demo_link", "--host", "https://beispiel.onrender.com/", stdout=out)
        lines = out.getvalue().splitlines()
        self.assertEqual(lines[0], "Fester Link (läuft nie ab): https://beispiel.onrender.com/demo/")
        first = lines[1].split(": ", 1)[1]
        self.assertTrue(first.startswith("https://beispiel.onrender.com/demo/"))
        self.assertTrue(demo.valid_token(first.rstrip("/").split("/")[-1]))
        self.assertIn("48 Stunden", out.getvalue())


class FixedLinkTests(TestCase):
    """Fester Link /demo/: Landingpage, Start per Klick, Platzvergabe nach Aktivität."""

    def test_get_creates_nothing_and_post_starts_demo(self):
        c = Client()
        r = c.get(reverse("demo_start"), REMOTE_ADDR="9.9.0.1")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Demo starten")
        self.assertIn("noindex", r["X-Robots-Tag"])
        self.assertEqual(User.objects.count(), 0)
        r = c.post(reverse("demo_start"), REMOTE_ADDR="9.9.0.1")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(User.objects.count(), 1)
        self.assertTrue(demo.is_demo(demo_user()))

    def test_idle_accounts_free_their_slot(self):
        for i in range(demo.MAX_ACCOUNTS):
            Client().post(reverse("demo_start"), REMOTE_ADDR=f"9.9.1.{i}")
        r = Client().post(reverse("demo_start"), REMOTE_ADDR="9.9.1.99")
        self.assertEqual(r.status_code, 503)
        User.objects.filter(username__endswith="@" + demo.DOMAIN).update(
            last_login=timezone.now() - timedelta(minutes=demo.IDLE_MINUTES + 1))
        r = Client().post(reverse("demo_start"), REMOTE_ADDR="9.9.1.98")
        self.assertEqual(r.status_code, 302)

    def test_total_cap(self):
        old = timezone.now() - timedelta(minutes=demo.IDLE_MINUTES + 1)
        for i in range(demo.MAX_TOTAL):
            u = demo.create_user()
            User.objects.filter(pk=u.pk).update(last_login=old)
        r = Client().post(reverse("demo_start"), REMOTE_ADDR="9.9.2.1")
        self.assertEqual(r.status_code, 503)

    def test_accounts_older_than_48h_are_deleted_on_start(self):
        u = demo.create_user()
        computed_run("single", "sma_cross", owner=u)
        User.objects.filter(pk=u.pk).update(date_joined=timezone.now() - timedelta(hours=demo.ACCOUNT_HOURS + 1))
        Client().post(reverse("demo_start"), REMOTE_ADDR="9.9.3.1")
        self.assertFalse(User.objects.filter(pk=u.pk).exists())
        self.assertFalse(BacktestRun.objects.filter(owner_id=u.pk).exists())

    def test_touch_writes_at_most_every_few_minutes(self):
        u = demo.create_user()
        demo.touch(u)
        first = User.objects.get(pk=u.pk).last_login
        demo.touch(u)
        self.assertEqual(User.objects.get(pk=u.pk).last_login, first)

    def test_kill_switch(self):
        with mock.patch.dict(os.environ, {"DEMO_ENABLED": "0"}):
            r = Client().post(reverse("demo_start"), REMOTE_ADDR="9.9.4.1")
        self.assertEqual(r.status_code, 503)
        self.assertEqual(User.objects.count(), 0)

    def test_real_user_is_not_replaced(self):
        User.objects.create_user("me@x.de", "me@x.de", PW)
        c = Client()
        c.login(username="me@x.de", password=PW)
        c.post(reverse("demo_start"))
        self.assertEqual(User.objects.count(), 1)


class DemoEnglishTests(TestCase):
    """Demo-Seiten haben im englischen Modus keinen deutschen Text."""

    def test_pages_in_english(self):
        c = Client()
        c.cookies["tb_lang"] = "en"
        pages = {"invalid": c.get(link("quatsch")), "disabled": None, "landing": c.get(reverse("demo_start"))}
        with mock.patch.dict(os.environ, {"DEMO_ENABLED": "0"}):
            pages["disabled"] = c.get(link())
        c.get(link())
        u = demo_user()
        run, _ = computed_run("single", "sma_cross", owner=u)
        run.source = "ccxt"
        run.save()
        pages["run"] = c.get(reverse("detail", args=[run.pk]))
        pages["account"] = c.get(reverse("account"))
        pages["paper"] = c.get(reverse("paper_list"))
        pages["share-blocked"] = c.post(reverse("share_toggle", args=[run.pk]), {"action": "on"}, follow=True)
        pages["signal-blocked"] = c.post(reverse("signal_toggle", args=[run.pk]), follow=True)
        pages["paper-blocked"] = c.post(reverse("paper_start", args=[run.pk]), {"capital": "100"}, follow=True)
        pages["email-blocked"] = c.post(reverse("email_change"), {"new_email": "a@b.de"}, follow=True)
        AiCall.objects.bulk_create([AiCall(user=u) for _ in range(5)])
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}):
            from backtester import ai
            ai.generate(run, u)
        pages["ai-limit"] = c.get(reverse("detail", args=[run.pk]))
        for i in range(demo.MAX_ACCOUNTS):
            Client().get(link(), REMOTE_ADDR=f"7.7.7.{i}")
        User.objects.create_superuser("admin@x.de", "admin@x.de", PW)
        ca = Client()
        ca.cookies["tb_lang"] = "en"
        ca.login(username="admin@x.de", password=PW)
        pages["admin-link"] = ca.get(reverse("demo_link"))
        with mock.patch.dict(os.environ, {"DEMO_ENABLED": "0"}):
            pages["admin-link-off"] = ca.get(reverse("demo_link"))
        pages["real-user-hint"] = ca.get(link(), follow=True)
        pages["full"] = c2 = Client()
        c2.cookies["tb_lang"] = "en"
        pages["full"] = c2.get(link(), REMOTE_ADDR="7.7.7.99")
        problems = {}
        for name, resp in pages.items():
            html = resp.content.decode()
            self.assertIn('<html lang="en"', html, name)
            for text in i18n_check.untranslated(html):
                problems.setdefault(text, []).append(name)
        if os.environ.get("I18N_DUMP"):
            open(os.environ["I18N_DUMP"], "w", encoding="utf-8").write("\n".join(problems))
        self.assertFalse(problems, f"{len(problems)} Texte ohne Übersetzung: {list(problems.items())[:6]}")
        self.assertContains(pages["run"], "Demo account")
