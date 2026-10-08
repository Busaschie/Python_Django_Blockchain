"""Tests: Paper-Trading (virtuelles Konto, Journal, Abgleich mit dem Backtest, Seiten und Zugriffsschutz)."""
from unittest import mock

import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from . import paper
from .models import PaperAccount, PaperEntry
from .test_extras import computed_run

User = get_user_model()
PW = "Sehr-gutes-Pw-17"


def small_frame(closes):
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D", tz="UTC")
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"open": c, "high": c, "low": c, "close": c, "volume": 1.0}, index=idx)


def account(user, **kw):
    base = dict(owner=user, name="t", chain="btc", symbol="BTC/USDT", timeframe="1d", source="ccxt", exchange="binance",
                strategy="sma_cross", params={"fast": 5, "slow": 20}, cost=0.001, execution="close",
                start_capital=10_000, cash=10_000)
    base.update(kw)
    return PaperAccount.objects.create(**base)


class HandComputedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("p@x.de", "p@x.de", PW)

    def run_signals(self, signals, closes, cost=0.01):
        acc = account(self.user, cost=cost)
        df = small_frame(closes)
        sig = pd.Series(signals, index=df.index)
        with mock.patch.object(paper, "_signal", side_effect=lambda a, d: sig.iloc[:len(d)]), \
                mock.patch.object(paper, "MIN_CANDLES", 1):
            for k in range(1, len(df) + 1):
                paper.check(acc, df=df.iloc[:k])
        acc.refresh_from_db()
        return acc

    def test_buy_and_sell_with_costs(self):
        acc = self.run_signals([0, 1, 1, 0, 0], [100, 100, 110, 120, 120])
        buy, sell = list(acc.entries.all())
        # Kauf bei Kerze 2 (Kurs 100): 10000 * (1 - 0,01) / 100 = 99 Einheiten
        self.assertEqual((buy.side, buy.price, round(buy.units, 6), round(buy.cost_paid, 2)), ("buy", 100.0, 99.0, 100.0))
        # Verkauf bei Kerze 4 (Kurs 120): 99 * 120 = 11880, minus 1 % = 11761,2
        self.assertEqual((sell.side, sell.price), ("sell", 120.0))
        self.assertAlmostEqual(acc.cash, 11761.2, places=2)
        self.assertEqual(acc.units, 0.0)
        self.assertAlmostEqual(sell.ret_pct, round((0.99 ** 2 * 1.2 - 1) * 100, 2), places=2)
        self.assertAlmostEqual(acc.return_pct, 17.61, places=2)

    def test_open_position_is_valued_at_last_price(self):
        acc = self.run_signals([0, 1, 1, 1], [100, 100, 110, 130])
        self.assertEqual(acc.position, 1)
        self.assertAlmostEqual(acc.equity(), 99.0 * 130, places=2)
        self.assertEqual(acc.entries.count(), 1)

    def test_same_candle_is_not_processed_twice(self):
        acc = account(self.user)
        df = small_frame([100, 101, 102])
        sig = pd.Series([1, 1, 1], index=df.index)
        with mock.patch.object(paper, "_signal", return_value=sig), mock.patch.object(paper, "MIN_CANDLES", 1):
            paper.check(acc, df=df)
            paper.check(acc, df=df)
            paper.check(acc, df=df)
        self.assertEqual(acc.entries.count(), 1)
        self.assertEqual(len(acc.equity_log), 1)

    def test_missed_signals_between_checks_are_not_traded(self):
        # Signal wechselt 0 -> 1 -> 0 zwischen zwei Prüfungen: keine Order (wie in der Praxis)
        # Prüfung nur bei Kerze 1 und 3 (Lücke):
        acc2 = account(self.user)
        df = small_frame([100, 110, 120])
        sig = pd.Series([0, 1, 0], index=df.index)
        with mock.patch.object(paper, "_signal", side_effect=lambda a, d: sig.iloc[:len(d)]), mock.patch.object(paper, "MIN_CANDLES", 1):
            paper.check(acc2, df=df.iloc[:1])
            paper.check(acc2, df=df.iloc[:3])
        self.assertEqual(acc2.entries.count(), 0)
        self.assertEqual(acc2.cash, 10_000)

    def test_equity_identity_after_every_order(self):
        acc = self.run_signals([1, 0, 1, 0, 1, 0, 1], [100, 90, 95, 105, 100, 110, 115])
        for e in acc.entries.all():
            self.assertGreater(e.equity_after, 0)
        self.assertEqual([e.side for e in acc.entries.all()], ["buy", "sell"] * 3 + ["buy"])


class RealStrategyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("p@x.de", "p@x.de", PW)
        cls.r, cls.df = computed_run("single", "sma_cross", owner=cls.user)
        cls.r.execution = "close"
        cls.r.save()

    def walk(self, step):
        acc = account(self.user, params=self.r.params, cost=self.r.fee + self.r.slippage)
        for k in range(600, len(self.df) + 1, step):
            paper.check(acc, df=self.df.iloc[:k])
        if len(self.df) % step:
            paper.check(acc, df=self.df)
        acc.refresh_from_db()
        return acc

    def test_paper_matches_backtest_when_checked_every_candle(self):
        acc = self.walk(1)
        rec = paper.reconcile(acc, df=self.df)
        self.assertTrue(rec["ok"], rec)
        self.assertLessEqual(abs(rec["diff_pct"]), 1.0, rec)
        self.assertEqual(rec["level"], "ok")

    def test_reconcile_reports_difference_with_gaps_and_open_execution(self):
        acc = self.walk(7)
        acc.execution = "open"
        rec = paper.reconcile(acc, df=self.df)
        self.assertTrue(rec["ok"])
        self.assertIn(rec["level"], ("ok", "warn", "bad"))
        self.assertTrue(any("Eröffnungskurs" in r for r in rec["reasons"]))

    def test_reconcile_needs_start(self):
        acc = account(self.user)
        self.assertFalse(paper.reconcile(acc, df=self.df)["ok"])


@override_settings(SIGNALS_ENABLED=True)
class PageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user("p@x.de", "p@x.de", PW)
        cls.other = User.objects.create_user("o@x.de", "o@x.de", PW)
        cls.r, cls.df = computed_run("single", "sma_cross", owner=cls.user)

    def setUp(self):
        self.client.force_login(self.user)
        self.patch = mock.patch.object(paper, "recent_df", return_value=self.df)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def start(self, **post):
        return self.client.post(reverse("paper_start", args=[self.r.pk]), post)

    def test_start_creates_account_and_detail_renders(self):
        r = self.start(capital="5000")
        acc = PaperAccount.objects.get(owner=self.user)
        self.assertRedirects(r, reverse("paper_detail", args=[acc.pk]))
        self.assertEqual(acc.start_capital, 5000)
        page = self.client.get(reverse("paper_detail", args=[acc.pk])).content.decode()
        for text in ("Backtest gegen Paper-Konto", "Journal", acc.name, "Abweichung"):
            self.assertIn(text, page)
        self.assertIn(acc.name, self.client.get(reverse("paper_list")).content.decode())

    def test_start_rejected_for_synthetic_bad_capital_and_limit(self):
        self.r.source = "synthetic"; self.r.save()
        self.start()
        self.assertEqual(PaperAccount.objects.count(), 0)
        self.r.source = "ccxt"; self.r.save()
        self.start(capital="5")
        self.start(capital="abc")                      # ungültig -> Standard 10000, aber...
        self.assertEqual(PaperAccount.objects.count(), 1)
        for _ in range(5):
            self.start()
        self.assertEqual(PaperAccount.objects.filter(active=True).count(), paper.max_accounts())

    def test_other_users_cannot_see_or_change_accounts(self):
        acc = account(self.other)
        for name in ("paper_detail", "paper_check", "paper_toggle", "paper_delete"):
            url = reverse(name, args=[acc.pk])
            resp = self.client.post(url) if name != "paper_detail" else self.client.get(url)
            self.assertEqual(resp.status_code, 404, name)
        other_run, _ = computed_run("single", "sma_cross", owner=self.other)
        self.assertEqual(self.client.post(reverse("paper_start", args=[other_run.pk])).status_code, 404)
        self.assertNotIn(acc.name + "x", self.client.get(reverse("paper_list")).content.decode())

    def test_check_toggle_delete(self):
        self.start()
        acc = PaperAccount.objects.get()
        acc.last_checked = None; acc.save()
        self.client.post(reverse("paper_check", args=[acc.pk]))
        self.client.post(reverse("paper_toggle", args=[acc.pk]))
        acc.refresh_from_db()
        self.assertFalse(acc.active)
        self.client.post(reverse("paper_delete", args=[acc.pk]))
        self.assertEqual(PaperAccount.objects.count(), 0)

    def test_cooldown_blocks_double_click(self):
        self.start()
        acc = PaperAccount.objects.get()
        self.assertEqual(paper.refresh(acc).get("skipped"), "cooldown")

    @override_settings(SIGNALS_ENABLED=False)
    def test_pages_hidden_when_signals_disabled(self):
        self.assertEqual(self.client.get(reverse("paper_list")).status_code, 404)

    def test_check_all_counts_and_survives_errors(self):
        account(self.user, name="a", params={"fast": 5, "slow": 20})
        account(self.user, name="b", chain="zzz")   # unbekannte Chain -> Fehler, stoppt die anderen nicht
        def fake(chain, tf, source, exchange, cache=None):
            if chain == "zzz":
                raise RuntimeError("Börse down")
            return self.df
        with mock.patch.object(paper, "recent_df", side_effect=fake):
            res = paper.check_all()
        self.assertEqual((res["checked"], res["errors"]), (1, 1))
        self.assertTrue(PaperAccount.objects.get(name="b").error)

    def test_dashboard_shows_start_button(self):
        self.assertIn("Paper-Trading starten", self.client.get(reverse("detail", args=[self.r.pk])).content.decode())
