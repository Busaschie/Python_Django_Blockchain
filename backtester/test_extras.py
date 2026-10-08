"""Tests: Parameter-Stabilitaet, Export (CSV/PDF) und aktuelles Signal (mit Ein-/Aus-Schalter)."""
import re
from datetime import datetime
from unittest import mock

import pandas as pd
from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from . import export, jobs, perf, signals, stability
from .engine import Risk
from .models import BacktestRun
from .strategies import STRATEGIES, default_inputs
from .test_plausibility import frame

User = get_user_model()


def computed_run(mode="single", strat="sma_cross", owner=None, **extra):
    df = frame(900, seed=3)
    run = BacktestRun(owner=owner, chain="btc", days=900, timeframe="1d", strategy=strat, fee=0.001, exchange="binance")
    run.created_at = datetime(2026, 1, 1)
    run.job = {**dict(chain="btc", timeframe="1d", execution="open", fee=0.001, slippage=0.0005, source="ccxt", mode=mode,
                      strategy=strat, days=900, train_frac=70, wf_folds=5, wf_train_mult=3), **default_inputs(strat), **extra}
    perf.clear_all()   # der Kurs-Cache kennt die Test-Attrappe nicht: nie Daten eines anderen Tests wiederverwenden
    with mock.patch.object(jobs, "fetch_ohlcv", return_value=(df, {"symbol": "BTC/USDT", "note": ""})):
        jobs.compute(run)
    run.execution, run.source = "open", "ccxt"
    run.save()
    return run, df


class StabilityTests(TestCase):
    def test_grid_and_center_for_each_strategy(self):
        for strat in ("sma_cross", "rsi", "combo"):
            run, _ = computed_run("single", strat)
            st = run.curves["stab"]
            self.assertTrue(st["ok"], st)
            self.assertEqual(len(st["ret"]), len(st["y"]))
            self.assertEqual(len(st["ret"][0]), len(st["x"]))
            self.assertIn(st["center"]["x"], st["x"])
            self.assertIn(st["center"]["y"], st["y"])
            self.assertIn(st["level"], ("ok", "warn", "bad"))
            self.assertTrue(st["verdict"])

    def test_center_matches_main_result(self):
        run, _ = computed_run("single", "sma_cross")
        st = run.curves["stab"]
        c = st["ret"][st["y"].index(st["center"]["y"])][st["x"].index(st["center"]["x"])]
        self.assertAlmostEqual(c, run.metrics["total_return_pct"], places=1)

    def test_split_uses_test_phase(self):
        run, _ = computed_run("split", "sma_cross")
        st = run.curves["stab"]
        self.assertTrue(st["ok"] and st["on_test"])
        c = st["ret"][st["y"].index(st["center"]["y"])][st["x"].index(st["center"]["x"])]
        self.assertAlmostEqual(c, run.validation["test"]["total_return_pct"], places=1)

    def test_walkforward_not_available(self):
        run, _ = computed_run("walkforward", "sma_cross")
        self.assertFalse(run.curves["stab"]["ok"])

    def test_invalid_cells_are_none(self):
        run, _ = computed_run("single", "sma_cross", param_a=40, param_b=45)
        st = run.curves["stab"]
        self.assertTrue(any(v is None for row in st["ret"] for v in row))   # fast >= slow ist ungueltig

    def test_verdict_levels(self):
        self.assertEqual(stability._verdict(20, [18, 15, 22, 19], 1.0, 18.5)[0], "ok")
        self.assertEqual(stability._verdict(40, [-5, 2, -8, -3], 0.25, -4)[0], "bad")
        self.assertEqual(stability._verdict(20, [15, -2, 12, -4], 0.5, 6)[0], "warn")
        self.assertEqual(stability._verdict(-3, [-5, 2], 0.5, -1.5)[0], "warn")


class ExportTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("a@b.de", "a@b.de", "Sehr-gutes-Pw-17")
        self.client.force_login(self.u)
        self.run, _ = computed_run("split", "sma_cross", owner=self.u)
        self.run.ai_comment = {"zusammenfassung": "Rendite 3,0 % ≥ Vergleich – ok", "staerken": ["a"], "schwaechen": ["b"],
                               "naechste_schritte": ["c"]}
        self.run.save()

    def test_csv(self):
        r = self.client.get(reverse("export_trades", args=[self.run.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertIn("attachment", r["Content-Disposition"])
        raw = r.content
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        lines = raw.decode("utf-8-sig").strip().split("\r\n")
        self.assertEqual(lines[0], "Kauf;Kaufpreis;Verkauf;Verkaufspreis;Netto %;Grund;Größe %")
        self.assertEqual(len(lines) - 1, len(self.run.curves["trades"]))
        if len(lines) > 1:
            cells = lines[1].split(";")
            self.assertEqual(len(cells), 7)
            self.assertRegex(cells[4], r"^-?\d+,\d\d$")   # Dezimalkomma

    def test_pdf(self):
        r = self.client.get(reverse("export_pdf", args=[self.run.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["Content-Type"], "application/pdf")
        self.assertTrue(r.content.startswith(b"%PDF"))
        self.assertGreater(len(r.content), 3000)

    def test_pdf_for_all_modes(self):
        for mode in ("single", "walkforward"):
            run, _ = computed_run(mode, "combo", owner=self.u)
            self.assertTrue(export.summary_pdf(run).startswith(b"%PDF"))

    def test_only_own_done_runs(self):
        other = User.objects.create_user("x@y.de", "x@y.de", "Sehr-gutes-Pw-17")
        self.client.force_login(other)
        for n in ("export_trades", "export_pdf"):
            self.assertEqual(self.client.get(reverse(n, args=[self.run.pk])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("export_pdf", args=[self.run.pk])).status_code, 302)   # Login noetig

    def test_links_on_page(self):
        html = self.client.get(reverse("detail", args=[self.run.pk])).content.decode()
        self.assertIn(reverse("export_trades", args=[self.run.pk]), html)
        self.assertIn(reverse("export_pdf", args=[self.run.pk]), html)
        self.assertIn('id="stabbox"', html)


def sig_df(up=True):
    """200 Kerzen; Aufwaerts- bzw. Abwaertstrend, damit SMA 20/50 eindeutig long bzw. flat ist."""
    n = 200
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    close = pd.Series([100 + (i if up else -i * 0.4) for i in range(n)], index=idx, dtype=float)
    return pd.DataFrame({"open": close, "high": close, "low": close, "close": close, "volume": 1.0})


class SignalTests(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("a@b.de", "a@b.de", "Sehr-gutes-Pw-17")
        self.client.force_login(self.u)
        self.run = BacktestRun.objects.create(owner=self.u, chain="btc", symbol="BTC/USDT", timeframe="1d", strategy="sma_cross",
                                              days=300, status="done", params={"fast": 20, "slow": 50}, source="ccxt")

    def patch(self, up):
        return mock.patch.object(signals, "fetch_ohlcv", return_value=(sig_df(up), {}))

    def test_current_long_and_flat(self):
        with self.patch(True):
            s = signals.current(self.run)
        self.assertTrue(s["ok"] and s["position"] == 1)
        with self.patch(False):
            s = signals.current(self.run)
        self.assertTrue(s["ok"] and s["position"] == 0)

    def test_synthetic_has_no_signal(self):
        self.run.source = "synthetic"
        self.assertFalse(signals.current(self.run)["ok"])

    @override_settings(SIGNALS_ENABLED=False, SIGNAL_CRON_TOKEN="t")
    def test_switch_off_hides_and_blocks_everything(self):
        html = self.client.get(reverse("detail", args=[self.run.pk])).content.decode()
        self.assertNotIn('id="sigbox"', html)
        self.assertEqual(self.client.post(reverse("signal_refresh", args=[self.run.pk])).status_code, 404)
        self.assertEqual(self.client.post(reverse("signal_toggle", args=[self.run.pk])).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("signal_check") + "?token=t").status_code, 404)

    @override_settings(SIGNALS_ENABLED=True)
    def test_refresh_and_display(self):
        with self.patch(True):
            self.client.post(reverse("signal_refresh", args=[self.run.pk]))
        self.run.refresh_from_db()
        self.assertEqual(self.run.signal_state["position"], 1)
        html = self.client.get(reverse("detail", args=[self.run.pk])).content.decode()
        self.assertIn('id="sigbox"', html)
        self.assertIn("LONG", html)

    @override_settings(SIGNALS_ENABLED=True, SIGNAL_MAX_PER_USER=1)
    def test_toggle_limit_and_off(self):
        with self.patch(True):
            self.client.post(reverse("signal_toggle", args=[self.run.pk]))
            self.run.refresh_from_db()
            self.assertTrue(self.run.signal_alert)
            self.assertEqual(self.run.signal_state["position"], 1)
            second = BacktestRun.objects.create(owner=self.u, chain="btc", symbol="BTC/USDT", timeframe="1d", strategy="sma_cross",
                                                days=300, status="done", params={"fast": 20, "slow": 50}, source="ccxt")
            self.client.post(reverse("signal_toggle", args=[second.pk]))
            second.refresh_from_db()
            self.assertFalse(second.signal_alert)          # Limit 1 erreicht
        self.client.post(reverse("signal_toggle", args=[self.run.pk]))
        self.run.refresh_from_db()
        self.assertFalse(self.run.signal_alert)

    @override_settings(SIGNALS_ENABLED=True, SIGNAL_MAX_PER_USER=5)
    def test_toggle_foreign_run_404(self):
        other = User.objects.create_user("x@y.de", "x@y.de", "Sehr-gutes-Pw-17")
        self.client.force_login(other)
        self.assertEqual(self.client.post(reverse("signal_toggle", args=[self.run.pk])).status_code, 404)

    @override_settings(SIGNALS_ENABLED=True)
    def test_check_all_mails_only_on_change(self):
        self.run.signal_alert, self.run.signal_state = True, {"ok": True, "position": 1}
        self.run.save()
        with self.patch(True):                     # unveraendert long -> keine Mail
            self.assertEqual(signals.check_all("https://x.de"), {"checked": 1, "sent": 0, "errors": 0})
        self.assertEqual(len(mail.outbox), 0)
        with self.patch(False):                    # Wechsel long -> flat -> Mail
            self.assertEqual(signals.check_all("https://x.de")["sent"], 1)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["a@b.de"])
        self.assertIn(f"/run/{self.run.pk}/", mail.outbox[0].body)
        with self.patch(False):                    # erneut flat -> nichts mehr
            self.assertEqual(signals.check_all("")["sent"], 0)
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(SIGNALS_ENABLED=True)
    def test_check_all_survives_errors_and_skips_inactive(self):
        self.run.signal_alert = True
        self.run.save()
        with mock.patch.object(signals, "fetch_ohlcv", side_effect=RuntimeError("Börse down")):
            self.assertEqual(signals.check_all("")["errors"], 1)
        self.u.is_active = False
        self.u.save()
        with self.patch(True):
            self.assertEqual(signals.check_all("")["checked"], 0)

    @override_settings(SIGNALS_ENABLED=True, SIGNAL_CRON_TOKEN="geheim")
    def test_cron_endpoint_token(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("signal_check")).status_code, 403)
        self.assertEqual(self.client.get(reverse("signal_check") + "?token=falsch").status_code, 403)
        r = self.client.get(reverse("signal_check") + "?token=geheim")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get(reverse("signal_check"), HTTP_AUTHORIZATION="Bearer geheim").status_code, 200)

    @override_settings(SIGNALS_ENABLED=True, SIGNAL_CRON_TOKEN="")
    def test_cron_endpoint_needs_configured_token(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("signal_check") + "?token=").status_code, 404)
