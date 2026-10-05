from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class AuthTests(TestCase):
    def test_app_requires_login(self):
        for url in ("/", "/status/", "/konto/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302)
            self.assertTrue(r["Location"].startswith(reverse("login")))

    def test_public_pages(self):
        for n in ("login", "register", "forgot"):
            self.assertEqual(self.client.get(reverse(n)).status_code, 200)
        self.assertIn(reverse("register"), self.client.get(reverse("login")).content.decode())

    def test_register_login_logout(self):
        r = self.client.post(reverse("register"), {"email": "A@b.de", "password1": "Sehr-gutes-Pw-17", "password2": "Sehr-gutes-Pw-17"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(username="a@b.de", email="a@b.de").exists())
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.post(reverse("logout"))
        self.assertEqual(self.client.get("/").status_code, 302)
        r = self.client.post(reverse("login"), {"username": "A@B.de", "password": "Sehr-gutes-Pw-17"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)

    def test_register_rejects_duplicate_and_weak(self):
        User.objects.create_user("a@b.de", "a@b.de", "x")
        r = self.client.post(reverse("register"), {"email": "a@b.de", "password1": "12345678", "password2": "12345678"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(User.objects.count(), 1)

    def test_reset_link_flow(self):
        import re
        u = User.objects.create_user("a@b.de", "a@b.de", "alt")
        r = self.client.post(reverse("forgot"), {"email": "a@b.de"})
        self.assertRedirects(r, reverse("forgot_done"))
        self.assertEqual(len(mail.outbox), 1)
        u.refresh_from_db()
        self.assertTrue(u.check_password("alt"))   # Passwort bleibt, bis der Link benutzt wird
        link = re.search(r"https?://[^/]+(/passwort-zuruecksetzen/\S+)", mail.outbox[0].body).group(1)
        r = self.client.get(link, follow=True)
        r = self.client.post(r.redirect_chain[-1][0], {"new_password1": "Neu-Passwort-42x", "new_password2": "Neu-Passwort-42x"})
        self.assertRedirects(r, reverse("reset_done"))
        u.refresh_from_db()
        self.assertTrue(u.check_password("Neu-Passwort-42x"))
        self.assertContains(self.client.get(link), "ungültig")   # Link nur einmal nutzbar
        # unbekannte Adresse: gleiche Weiterleitung, keine Mail
        r = self.client.post(reverse("forgot"), {"email": "x@y.de"})
        self.assertRedirects(r, reverse("forgot_done"))
        self.assertEqual(len(mail.outbox), 1)

    def test_runs_are_private(self):
        from backtester.models import BacktestRun
        a = User.objects.create_user("a@b.de", "a@b.de", "pw-Aaaa-1234")
        b = User.objects.create_user("b@b.de", "b@b.de", "pw-Bbbb-1234")
        mk = lambda o, batch="": BacktestRun.objects.create(
            owner=o, chain="btc", symbol="BTC/USDT", timeframe="1d", strategy="rsi", days=10, batch=batch, status="done")
        ra, rb = mk(a, "xyz"), mk(b, "uvw")
        self.client.force_login(a)
        self.assertEqual(self.client.get(f"/run/{ra.pk}/").status_code, 200)
        self.assertEqual(self.client.get(f"/run/{rb.pk}/").status_code, 404)
        self.assertEqual(self.client.get("/vergleich/uvw/").status_code, 404)
        html = self.client.get("/").content.decode()
        self.assertIn(f"/run/{ra.pk}/", html)
        self.assertNotIn(f"/run/{rb.pk}/", html)
        js = self.client.get(f"/status/?ids={ra.pk},{rb.pk}").json()
        self.assertEqual([x["id"] for x in js["runs"]], [ra.pk])

    def test_change_password(self):
        User.objects.create_user("a@b.de", "a@b.de", "Alt-Passwort-91")
        self.client.login(username="a@b.de", password="Alt-Passwort-91")
        r = self.client.post(reverse("account"), {"old_password": "Alt-Passwort-91",
                             "new_password1": "Neu-Passwort-42x", "new_password2": "Neu-Passwort-42x"})
        self.assertRedirects(r, reverse("account"))
        self.assertEqual(self.client.get("/").status_code, 200)   # Sitzung bleibt
        self.assertTrue(User.objects.get().check_password("Neu-Passwort-42x"))


class MailApiTests(TestCase):
    def test_brevo_payload(self):
        import os
        from unittest import mock
        from django.core.mail import EmailMessage
        from accounts import mailapi
        with mock.patch.dict(os.environ, {"EMAIL_API": "brevo", "EMAIL_API_KEY": "k"}), \
                mock.patch.object(mailapi, "_post", return_value=201) as p:
            n = mailapi.ApiEmailBackend().send_messages([EmailMessage("Betreff", "Text", "Trading <t@x.de>", ["a@b.de"])])
        self.assertEqual(n, 1)
        url, headers, payload = p.call_args.args
        self.assertEqual(headers, {"api-key": "k"})
        self.assertEqual(payload["sender"], {"email": "t@x.de", "name": "Trading"})
        self.assertEqual(payload["to"], [{"email": "a@b.de"}])


class AdminTests(TestCase):
    def test_admin_login_page_and_access(self):
        self.assertEqual(self.client.get("/admin/login/").status_code, 200)
        User.objects.create_user("n@b.de", "n@b.de", "pw-Aaaa-1234")
        self.client.login(username="n@b.de", password="pw-Aaaa-1234")
        self.assertEqual(self.client.get("/admin/").status_code, 302)   # normaler Benutzer: kein Zugang
        User.objects.create_superuser("a@b.de", "a@b.de", "pw-Aaaa-1234")
        self.client.login(username="a@b.de", password="pw-Aaaa-1234")
        r = self.client.get("/admin/")
        self.assertEqual(r.status_code, 200)

    def test_ensure_admin(self):
        import os
        from unittest import mock
        from config import automigrate
        with mock.patch.dict(os.environ, {"ADMIN_EMAIL": "Root@B.de", "ADMIN_PASSWORD": "pw-Aaaa-1234"}):
            automigrate.ensure_admin()
            u = User.objects.get(username="root@b.de")
            self.assertTrue(u.is_superuser and u.check_password("pw-Aaaa-1234"))
            with mock.patch.dict(os.environ, {"ADMIN_PASSWORD": "anders"}):
                automigrate.ensure_admin()   # bestehendes Passwort bleibt
            u.refresh_from_db()
            self.assertTrue(u.check_password("pw-Aaaa-1234"))
