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

    def _register(self, email="A@b.de", pw="Sehr-gutes-Pw-17"):
        import re
        r = self.client.post(reverse("register"), {"email": email})
        self.assertRedirects(r, reverse("register_sent"))
        self.assertEqual(User.objects.count(), 0)   # noch kein Konto
        self.assertEqual(len(mail.outbox), 1)
        link = re.search(r"https?://[^/]+(/registrieren/bestaetigen/\S+)", mail.outbox[0].body).group(1)
        self.assertEqual(self.client.get(link).status_code, 200)
        self.assertEqual(User.objects.count(), 0)   # Link-Klick allein legt nichts an
        return link

    def test_register_login_logout(self):
        link = self._register()
        r = self.client.post(link, {"password1": "Sehr-gutes-Pw-17", "password2": "Sehr-gutes-Pw-17"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertTrue(User.objects.filter(username="a@b.de", email="a@b.de", is_active=True).exists())
        self.assertEqual(self.client.get("/").status_code, 200)
        self.client.post(reverse("logout"))
        self.assertEqual(self.client.get("/").status_code, 302)
        r = self.client.post(reverse("login"), {"username": "A@B.de", "password": "Sehr-gutes-Pw-17"})
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        # Link ist einmalig
        self.client.post(reverse("logout"))
        self.assertEqual(self.client.get(link).status_code, 400)

    def test_register_weak_password_and_mismatch(self):
        link = self._register()
        self.assertEqual(self.client.post(link, {"password1": "12345678", "password2": "12345678"}).status_code, 200)
        self.assertEqual(self.client.post(link, {"password1": "Sehr-gutes-Pw-17", "password2": "anders-17-Pw-x"}).status_code, 200)
        self.assertEqual(User.objects.count(), 0)

    def test_register_bad_or_expired_link(self):
        from django.core import signing
        self.assertEqual(self.client.get("/registrieren/bestaetigen/muell/").status_code, 400)
        tok = signing.dumps("x@y.de", salt="accounts.register")
        with self.settings():
            from unittest import mock
            with mock.patch("accounts.views.MAX_AGE", -1):
                self.assertEqual(self.client.get(reverse("register_confirm", args=[tok])).status_code, 400)
        # Token mit anderem Salt wird abgelehnt
        self.assertEqual(self.client.get(reverse("register_confirm", args=[signing.dumps("x@y.de", salt="anderes")])).status_code, 400)

    def test_register_existing_email_sends_hint_mail(self):
        User.objects.create_user("a@b.de", "a@b.de", "x")
        r = self.client.post(reverse("register"), {"email": "A@b.de"})
        self.assertRedirects(r, reverse("register_sent"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("bereits ein Konto", mail.outbox[0].body)
        self.assertIn(reverse("forgot"), mail.outbox[0].body)
        self.assertNotIn("bestaetigen", mail.outbox[0].body)
        self.assertEqual(User.objects.count(), 1)

    def test_register_after_user_deleted(self):
        u = User.objects.create_user("a@b.de", "a@b.de", "x")
        u.delete()
        self.client.post(reverse("register"), {"email": "a@b.de"})
        self.assertIn("bestaetigen", mail.outbox[0].body)

    def test_register_invalid_email(self):
        self.assertEqual(self.client.post(reverse("register"), {"email": "keine-mail"}).status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

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


class DeleteRunTests(TestCase):
    def test_delete_own_only(self):
        from backtester.models import BacktestRun
        a = User.objects.create_user("a@b.de", "a@b.de", "pw-Aaaa-1234")
        b = User.objects.create_user("b@b.de", "b@b.de", "pw-Bbbb-1234")
        mk = lambda o: BacktestRun.objects.create(owner=o, chain="btc", symbol="BTC/USDT", timeframe="1d",
                                                  strategy="rsi", days=10, status="done")
        ra, rb, rc = mk(a), mk(b), mk(a)
        self.client.force_login(a)
        self.assertEqual(self.client.get(f"/loeschen/{ra.pk}/").status_code, 405)      # nur POST
        self.assertEqual(self.client.post(f"/loeschen/{rb.pk}/").status_code, 404)     # fremder Lauf
        self.assertTrue(BacktestRun.objects.filter(pk=rb.pk).exists())
        self.assertIn(f"/loeschen/{ra.pk}/", self.client.get("/").content.decode())
        self.assertNotIn(f"/loeschen/{rb.pk}/", self.client.get("/").content.decode())
        r = self.client.post(f"/loeschen/{ra.pk}/", {"next": f"/run/{ra.pk}/"})        # gerade angezeigter Lauf
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        r = self.client.post(f"/loeschen/{rc.pk}/", {"next": "https://evil.example/"})   # kein Open Redirect
        self.assertRedirects(r, "/", fetch_redirect_response=False)
        self.assertFalse(BacktestRun.objects.filter(owner=a).exists())


class AccountManageTests(TestCase):
    PW = "Sehr-gutes-Pw-17"

    def setUp(self):
        self.u = User.objects.create_user("a@b.de", "a@b.de", self.PW)
        self.client.force_login(self.u)

    def _link(self, n=0):
        import re
        return re.search(r"https?://[^/]+(/konto/email/bestaetigen/\S+)", mail.outbox[n].body).group(1)

    def test_page_has_all_forms(self):
        c = self.client.get(reverse("account")).content.decode()
        for url in ("email_change", "account_delete", "account"):
            self.assertIn(reverse(url), c)

    def test_email_change_flow(self):
        r = self.client.post(reverse("email_change"), {"new_email": "N@b.de", "current_password": self.PW})
        self.assertRedirects(r, reverse("account"))
        self.assertEqual(mail.outbox[0].to, ["n@b.de"])
        self.u.refresh_from_db()
        self.assertEqual(self.u.username, "a@b.de")        # noch unverändert
        self.assertRedirects(self.client.get(self._link()), reverse("account"))
        self.u.refresh_from_db()
        self.assertEqual((self.u.username, self.u.email), ("n@b.de", "n@b.de"))
        self.client.post(reverse("logout"))
        r = self.client.post(reverse("login"), {"username": "n@b.de", "password": self.PW})
        self.assertRedirects(r, "/", fetch_redirect_response=False)

    def test_email_change_validation(self):
        User.objects.create_user("x@b.de", "x@b.de", "y")
        for data in ({"new_email": "n@b.de", "current_password": "falsch"},
                     {"new_email": "X@b.de", "current_password": self.PW},
                     {"new_email": "a@b.de", "current_password": self.PW},
                     {"new_email": "kaputt", "current_password": self.PW}):
            self.assertEqual(self.client.post(reverse("email_change"), data).status_code, 200)
        self.assertEqual(len(mail.outbox), 0)

    def test_email_change_link_other_user_or_taken_or_bad(self):
        self.client.post(reverse("email_change"), {"new_email": "n@b.de", "current_password": self.PW})
        link = self._link()
        other = User.objects.create_user("o@b.de", "o@b.de", "pw-1234-xyz")
        self.client.force_login(other)
        self.client.get(link)
        other.refresh_from_db()
        self.assertEqual(other.username, "o@b.de")          # fremder Link wirkt nicht
        self.client.force_login(self.u)
        User.objects.create_user("n@b.de", "n@b.de", "pw-1234-xyz")   # inzwischen vergeben
        self.client.get(link)
        self.u.refresh_from_db()
        self.assertEqual(self.u.username, "a@b.de")
        self.client.get("/konto/email/bestaetigen/muell/")
        self.u.refresh_from_db()
        self.assertEqual(self.u.username, "a@b.de")

    def test_email_change_link_requires_login(self):
        self.client.post(reverse("email_change"), {"new_email": "n@b.de", "current_password": self.PW})
        link = self._link()
        self.client.post(reverse("logout"))
        self.assertEqual(self.client.get(link).status_code, 302)
        self.assertTrue(self.client.get(link)["Location"].startswith(reverse("login")))

    def test_delete_account(self):
        from backtester.models import BacktestRun
        BacktestRun.objects.create(owner=self.u, chain="btc", symbol="BTC/USDT", timeframe="1d", strategy="rsi", days=10, status="done")
        # falsches Passwort / keine Bestätigung -> nichts passiert
        self.assertEqual(self.client.post(reverse("account_delete"), {"current_password": "x", "confirm": "on"}).status_code, 200)
        self.assertEqual(self.client.post(reverse("account_delete"), {"current_password": self.PW}).status_code, 200)
        self.assertTrue(User.objects.filter(pk=self.u.pk).exists())
        r = self.client.post(reverse("account_delete"), {"current_password": self.PW, "confirm": "on"})
        self.assertRedirects(r, reverse("login"))
        self.assertFalse(User.objects.filter(pk=self.u.pk).exists())
        self.assertEqual(BacktestRun.objects.count(), 0)
        self.assertEqual(self.client.get("/").status_code, 302)   # ausgeloggt

    def test_delete_and_email_need_post(self):
        self.assertEqual(self.client.get(reverse("account_delete")).status_code, 405)
        self.assertEqual(self.client.get(reverse("email_change")).status_code, 405)


class ThrottleAndLegalTests(TestCase):
    def test_login_locks_after_5_failures_even_with_right_password(self):
        User.objects.create_user("a@b.de", "a@b.de", "Sehr-gutes-Pw-17")
        for _ in range(5):
            self.assertEqual(self.client.post(reverse("login"), {"username": "a@b.de", "password": "falsch"}).status_code, 200)
        r = self.client.post(reverse("login"), {"username": "a@b.de", "password": "Sehr-gutes-Pw-17"})
        self.assertEqual(r.status_code, 429)
        self.assertContains(r, "Zu viele Fehlversuche", status_code=429)
        self.assertEqual(self.client.get("/").status_code, 302)   # nicht angemeldet
        self.assertEqual(self.client.post(reverse("login"), {"username": "x@y.de", "password": "x"}).status_code, 200)  # anderes Konto frei

    def test_login_success_resets_counter(self):
        User.objects.create_user("a@b.de", "a@b.de", "Sehr-gutes-Pw-17")
        for _ in range(4):
            self.client.post(reverse("login"), {"username": "a@b.de", "password": "falsch"})
        self.assertEqual(self.client.post(reverse("login"), {"username": "a@b.de", "password": "Sehr-gutes-Pw-17"}).status_code, 302)
        self.client.post(reverse("logout"))
        for _ in range(4):
            self.client.post(reverse("login"), {"username": "a@b.de", "password": "falsch"})
        self.assertEqual(self.client.post(reverse("login"), {"username": "a@b.de", "password": "Sehr-gutes-Pw-17"}).status_code, 302)

    def test_register_and_forgot_limited_per_email(self):
        for name in ("register", "forgot"):
            for _ in range(3):
                self.assertEqual(self.client.post(reverse(name), {"email": "n@b.de"}).status_code, 302)
            self.assertEqual(self.client.post(reverse(name), {"email": "n@b.de"}).status_code, 429)
        self.assertEqual(len(mail.outbox), 3)   # forgot: unbekannte Adresse sendet nichts; register: 3 Mails, die 4. blockiert

    def test_old_attempts_expire(self):
        from datetime import timedelta
        from django.utils import timezone
        from .models import Attempt
        User.objects.create_user("a@b.de", "a@b.de", "Sehr-gutes-Pw-17")
        for _ in range(5):
            self.client.post(reverse("login"), {"username": "a@b.de", "password": "falsch"})
        Attempt.objects.update(created=timezone.now() - timedelta(minutes=16))
        self.assertEqual(self.client.post(reverse("login"), {"username": "a@b.de", "password": "Sehr-gutes-Pw-17"}).status_code, 302)

    def test_legal_pages_public_with_footer(self):
        import os
        from unittest import mock
        for n in ("impressum", "datenschutz"):
            r = self.client.get(reverse(n))
            self.assertEqual(r.status_code, 200)
            self.assertContains(r, "LEGAL_NAME")   # Warnung, solange nicht gesetzt
        with mock.patch.dict(os.environ, {"LEGAL_NAME": "Max Muster", "LEGAL_STREET": "Weg 1", "LEGAL_CITY": "80331 München", "LEGAL_EMAIL": "m@x.de"}):
            r = self.client.get(reverse("impressum"))
            self.assertContains(r, "Max Muster")
            self.assertNotContains(r, "LEGAL_NAME")
        self.assertContains(self.client.get(reverse("login")), reverse("datenschutz"))
        self.assertContains(self.client.get(reverse("register")), reverse("datenschutz"))
