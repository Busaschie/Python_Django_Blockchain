"""Tests: Hinweis 'keine Anlageberatung', Datenschutz-/Cookie-Check, Admin-Login-Drosselung, Datenexport."""
import json
import re
from pathlib import Path

from django.contrib.auth import get_user_model
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from accounts import throttle
from accounts.models import Attempt
from backtester.models import BacktestRun

User = get_user_model()
PW = "Sehr-gutes-Pw-17"
ROOT = Path(__file__).resolve().parent.parent
ALLOWED_COOKIES = {"sessionid", "csrftoken", "tb_lang"}


class AdminThrottleTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_superuser("admin@x.de", "admin@x.de", PW)

    def post(self, user="admin@x.de", pw="falsch", ip="10.0.0.1", client=None):
        return (client or self.client).post(reverse("admin_login"), {"username": user, "password": pw, "next": "/admin/"},
                                            REMOTE_ADDR=ip)

    def test_login_page_and_successful_login_work(self):
        self.assertEqual(self.client.get(reverse("admin_login")).status_code, 200)
        r = self.post(pw=PW)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.client.get("/admin/").status_code, 200)

    def test_blocked_after_five_wrong_passwords_per_account(self):
        for i in range(5):
            self.assertEqual(self.post(ip=f"10.0.1.{i}").status_code, 200)      # Fehler-Formular, wechselnde IPs
        r = self.post(pw=PW, ip="10.0.9.9")                                    # sogar das richtige Passwort ist jetzt gesperrt
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r["Retry-After"], "900")
        self.assertIn("Admin-Login", r.content.decode())
        self.assertEqual(self.client.get("/admin/").status_code, 302)           # nicht angemeldet

    def test_blocked_after_ten_attempts_per_ip_with_changing_names(self):
        for i in range(10):
            self.assertEqual(self.post(user=f"x{i}@x.de", ip="10.0.2.2").status_code, 200)
        self.assertEqual(self.post(user="neu@x.de", ip="10.0.2.2").status_code, 429)
        self.assertEqual(self.post(user="neu@x.de", ip="10.0.2.3").status_code, 200)   # andere IP nicht betroffen

    def test_success_resets_account_counter(self):
        for _ in range(4):
            self.post()
        self.assertEqual(self.post(pw=PW, ip="10.0.3.3").status_code, 302)
        self.assertEqual(Attempt.objects.filter(scope="admin", key="user:admin@x.de").count(), 0)

    def test_non_staff_user_cannot_enter_and_is_counted(self):
        User.objects.create_user("n@x.de", "n@x.de", PW)
        r = self.post(user="n@x.de", pw=PW)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Attempt.objects.filter(scope="admin", key="user:n@x.de").count(), 1)

    def test_normal_login_counters_are_separate(self):
        for _ in range(5):
            self.post()
        self.assertFalse(throttle.blocked(_Req("10.0.8.8"), "login", "admin@x.de"))

    def test_get_is_never_blocked(self):
        for _ in range(6):
            self.post()
        self.assertEqual(self.client.get(reverse("admin_login")).status_code, 200)


class _Req:
    def __init__(self, ip):
        self.META = {"REMOTE_ADDR": ip}


class DisclaimerTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("me@x.de", "me@x.de", PW)

    def test_disclaimer_on_every_kind_of_page(self):
        anon = Client()
        for name in ("login", "impressum", "datenschutz", "register", "forgot"):
            page = anon.get(reverse(name)).content.decode()
            self.assertIn('id="disclaimer"', page, name)
            self.assertIn("Keine Anlageberatung", page, name)
        self.client.force_login(self.user)
        for name in ("index", "account"):
            self.assertIn("Keine Anlageberatung", self.client.get(reverse(name)).content.decode(), name)

    def test_disclaimer_on_shared_page(self):
        run = BacktestRun.objects.create(owner=self.user, chain="btc", symbol="BTC/USDT", timeframe="1d", strategy="sma_cross",
                                         days=100, status="done", share_token="t" * 24)
        page = Client().get(reverse("shared", args=[run.share_token])).content.decode()
        self.assertIn("Keine Anlageberatung", page)


class PrivacyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("me@x.de", "me@x.de", PW)

    def test_no_third_party_resources_in_templates(self):
        """Keine Skripte, Stylesheets, Bilder, Schriften oder iframes von fremden Servern (DSGVO: IP-Weitergabe)."""
        bad = []
        for path in list(ROOT.glob("**/templates/**/*.html")) + list(ROOT.glob("**/templates/**/*.txt")):
            text = path.read_text(encoding="utf-8")
            for tag in re.findall(r"<(?:script|link|img|iframe|source|video|audio|embed|object)\b[^>]*>", text, re.I):
                if re.search(r"(?:src|href|data)\s*=\s*[\"']?(?:https?:)?//", tag, re.I):
                    bad.append((path.name, tag[:80]))
            if re.search(r"@import\s+url\(|url\(\s*[\"']?https?://", text):
                bad.append((path.name, "CSS-Import/URL"))
            if re.search(r"fetch\(\s*[\"']https?://|new WebSocket\(\s*[\"']wss?://|new XMLHttpRequest", text):
                bad.append((path.name, "Skript-Anfrage nach außen"))
        self.assertEqual(bad, [])

    def test_plotly_is_served_from_own_origin(self):
        self.client.force_login(self.user)
        page = self.client.get(reverse("index")).content.decode()
        self.assertNotIn("cdn.plot.ly", page)
        self.assertTrue((ROOT / "backtester/static/backtester/plotly-2.35.2.min.js").exists())

    def test_only_documented_cookies_with_safe_flags(self):
        c = Client()
        c.get(reverse("login"))
        r = c.post(reverse("login"), {"username": "me@x.de", "password": PW})
        self.assertEqual(r.status_code, 302)
        c.get(reverse("index"))
        names = set(c.cookies.keys())
        self.assertTrue(names <= ALLOWED_COOKIES, names)
        self.assertIn("sessionid", names)
        self.assertTrue(c.cookies["sessionid"]["httponly"])
        self.assertEqual(c.cookies["sessionid"]["samesite"], "Lax")
        self.assertEqual(c.cookies["csrftoken"]["samesite"], "Lax")

    def test_shared_page_sets_no_cookie(self):
        run = BacktestRun.objects.create(owner=self.user, chain="btc", symbol="BTC/USDT", timeframe="1d", strategy="sma_cross",
                                         days=100, status="done", share_token="s" * 24)
        c = Client()
        c.get(reverse("shared", args=[run.share_token]))
        self.assertEqual(set(c.cookies.keys()), set())

    def test_privacy_text_documents_cookies_and_processors(self):
        page = Client().get(reverse("datenschutz")).content.decode()
        for name in ALLOWED_COOKIES | {"tb_theme"}:
            self.assertIn(name, page, name)
        for provider in ("Render", "Neon", "Groq", "Brevo", "Resend"):
            self.assertIn(provider, page, provider)
        self.assertNotIn("cdn.plot.ly", page)
        self.assertIn("Meine Daten herunterladen", page)

    def test_response_headers(self):
        self.client.force_login(self.user)
        r = self.client.get(reverse("index"))
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r["X-Frame-Options"], "DENY")
        self.assertEqual(r["Referrer-Policy"], "same-origin")

    def test_shared_page_has_noindex(self):
        run = BacktestRun.objects.create(owner=self.user, chain="btc", symbol="BTC/USDT", timeframe="1d", strategy="sma_cross",
                                         days=100, status="done", share_token="n" * 24)
        r = Client().get(reverse("shared", args=[run.share_token]))
        self.assertIn("noindex", r["X-Robots-Tag"])


class ExportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("me@x.de", "me@x.de", PW)
        cls.other = User.objects.create_user("o@x.de", "o@x.de", PW)
        BacktestRun.objects.create(owner=cls.user, chain="btc", symbol="BTC/USDT", timeframe="1d", strategy="sma_cross", days=100,
                                   status="done", tags="|alpha|", favorite=True, metrics={"sharpe": 1.2},
                                   curves={"trades": [{"ret_pct": 2.0}], "close": [1, 2, 3]})
        BacktestRun.objects.create(owner=cls.other, chain="eth", symbol="ETH/USDT", timeframe="1d", strategy="rsi", days=50, status="done")

    def test_export_contains_own_data_only_without_secrets(self):
        self.client.force_login(self.user)
        r = self.client.post(reverse("account_export"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])
        data = json.loads(r.content)
        self.assertEqual(data["konto"]["email"], "me@x.de")
        self.assertEqual(len(data["auswertungen"]), 1)
        run = data["auswertungen"][0]
        self.assertEqual((run["tags"], run["favorit"], run["trades"]), (["alpha"], True, [{"ret_pct": 2.0}]))
        self.assertNotIn("[1, 2, 3]", json.dumps(run))            # keine Kursreihen
        raw = r.content.decode()
        self.assertNotIn("pbkdf2", raw)
        self.assertNotIn("o@x.de", raw)
        self.assertNotIn("ETH", raw)

    def test_export_requires_login_and_post(self):
        self.assertEqual(Client().post(reverse("account_export")).status_code, 302)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("account_export")).status_code, 405)

    def test_account_page_links_export(self):
        self.client.force_login(self.user)
        self.assertIn("Meine Daten herunterladen", self.client.get(reverse("account")).content.decode())


class MailLanguageTests(TestCase):
    """Reset- und Bestätigungsmails folgen der gewählten Sprache."""

    @classmethod
    def setUpTestData(cls):
        User.objects.create_user("me@x.de", "me@x.de", PW)

    def _reset(self, lang):
        from django.core import mail
        mail.outbox.clear()
        c = Client()
        if lang:
            c.cookies["tb_lang"] = lang
        c.post(reverse("forgot"), {"email": "me@x.de"}, REMOTE_ADDR="10.7.7.7")
        return mail.outbox[0]

    def test_reset_mail_english(self):
        m = self._reset("en")
        self.assertIn("reset password", m.subject)
        self.assertIn("a password reset was requested", m.body)
        self.assertNotIn("Zurücksetzen", m.body)

    def test_reset_mail_german_default(self):
        m = self._reset(None)
        self.assertIn("Passwort zurücksetzen", m.subject)
        self.assertIn("Zurücksetzen des Passworts", m.body)
