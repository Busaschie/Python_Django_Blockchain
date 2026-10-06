import json
import os
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from . import ai
from .models import AiCall, BacktestRun

User = get_user_model()
CURVES = {
    "plaus": {"level": "warn", "checks": [{"title": "Anzahl Trades", "detail": "10 Trades (unter 30)", "level": "warn"}]},
    "mc": {"ok": True, "n_trades": 10, "prob_profit": 80.1, "return_p5": -19.66, "return_p50": 34.04, "return_p95": 145.45,
           "dd_median": -15.02, "dd_p95": -31.59, "random_median": 44.84, "p_value": 0.554, "beats_random": False},
    "regimes": {"ok": True, "hint": "Am besten in Aufwärts-Phasen.", "rows": [
        {"label": "Aufwärts", "share_pct": 64.2, "strategy_pct": 74.79, "buyhold_pct": 576.1, "trades": 6}]},
    "cost": {"ok": True, "rows": [{"mult": 1, "total_return_pct": 33.96}], "break_even_mult": None, "verdict": "Kostenrobust."},
}
GOOD = {"zusammenfassung": "Die Strategie erzielte 33,96 % gegenüber 189,33 % bei Buy & Hold.", "staerken": ["Kostenrobust."],
        "schwaechen": ["Nur 10 Trades, daher nicht belastbar."], "naechste_schritte": ["Zeitraum verlängern, um mehr Trades zu erhalten."]}


def make_run(owner=None):
    return BacktestRun.objects.create(
        owner=owner, chain="btc", symbol="BTC/USDT", timeframe="1d", strategy="sma_cross", days=900, status="done",
        metrics={"total_return_pct": 33.96, "buyhold_return_pct": 189.33, "max_drawdown_pct": -24.92,
                 "buyhold_max_drawdown_pct": -60.0, "trades": 10, "sharpe": 0.54}, curves=CURVES, params={"fast": 20, "slow": 50})


class PureTests(SimpleTestCase):
    def test_number_check(self):
        text = "Rendite 33,96 und Drawdown -24.92, Buy & Hold 189,33, 12 Trades, 10 davon"
        self.assertEqual(ai.unknown_numbers("Rendite 33,96 % bei -24,92 %, 5 Schritte, 90 Tage", text), [])
        self.assertEqual(ai.unknown_numbers("Rendite 34 %", text), [])          # sinnvoll gerundet
        self.assertEqual(ai.unknown_numbers("Rendite 41,7 %", text), [41.7])      # erfunden
        self.assertEqual(ai.unknown_numbers("Rendite 40 %", text), [40.0])

    def test_parse_answer(self):
        out = ai.parse_answer(json.dumps({**GOOD, "zusammenfassung": "**Fett** Text"}))
        self.assertEqual(out["zusammenfassung"], "Fett Text")
        self.assertEqual(ai.parse_answer("Hier: " + json.dumps(GOOD))["staerken"], ["Kostenrobust."])
        with self.assertRaises(ValueError):
            ai.parse_answer("{}")
        with self.assertRaises(ValueError):
            ai.parse_answer("kein json")


class FlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("a@b.de", "a@b.de", "pw-Aaaa-1234")
        self.client.force_login(self.user)
        self.run_ = make_run(self.user)
        env = mock.patch.dict(os.environ, {"GROQ_API_KEY": "k"}, clear=False)
        env.start(); self.addCleanup(env.stop)

    def test_facts_contain_all_blocks_and_no_user_data(self):
        t = ai.facts_text(ai.facts(self.run_))
        for key in ("kennzahlen", "plausibilitaet", "monte_carlo", "marktphasen", "kosten_test"):
            self.assertIn(key, t)
        self.assertNotIn("a@b.de", t)

    def test_llm_success_is_stored_once(self):
        with mock.patch.object(ai, "chat", return_value=json.dumps(GOOD)) as chat:
            r = self.client.post(f"/kommentar/{self.run_.pk}/")
            self.assertRedirects(r, f"/run/{self.run_.pk}/")
            self.client.post(f"/kommentar/{self.run_.pk}/")        # zweiter Klick: kein neuer Aufruf
        self.assertEqual(chat.call_count, 1)
        self.run_.refresh_from_db()
        self.assertEqual(self.run_.ai_source, "groq:openai/gpt-oss-120b")
        self.assertEqual(AiCall.objects.filter(user=self.user, ok=True).count(), 1)
        page = self.client.get(f"/run/{self.run_.pk}/").content.decode()
        self.assertIn("33,96 % gegenüber 189,33 %", page)
        self.assertIn("keine Anlageberatung", page)
        self.assertNotIn("Auswertung kommentieren</button>", page)

    def test_invented_number_triggers_retry_then_fallback_model(self):
        bad = {**GOOD, "zusammenfassung": "Die Rendite betrug 77,7 %."}
        replies = [json.dumps(bad), json.dumps(bad), json.dumps(GOOD)]   # 120b: falsch, falsch -> 20b: gut
        with mock.patch.object(ai, "chat", side_effect=replies) as chat:
            self.client.post(f"/kommentar/{self.run_.pk}/")
        self.assertEqual(chat.call_count, 3)
        self.run_.refresh_from_db()
        self.assertEqual(self.run_.ai_source, "groq:openai/gpt-oss-20b")
        self.assertEqual(AiCall.objects.count(), 1)      # ein Klick = ein Aufruf im Limit

    def test_api_failure_falls_back_to_rules_and_allows_retry(self):
        with mock.patch.object(ai, "chat", side_effect=ai.AiError("429")):
            self.client.post(f"/kommentar/{self.run_.pk}/")
        self.run_.refresh_from_db()
        self.assertEqual(self.run_.ai_source, "regeln")
        self.assertIn("regelbasierter Kommentar", self.run_.ai_comment["note"])
        self.assertTrue(self.run_.ai_comment["naechste_schritte"])
        self.assertIn("Mit KI neu erstellen", self.client.get(f"/run/{self.run_.pk}/").content.decode())
        with mock.patch.object(ai, "chat", return_value=json.dumps(GOOD)):
            self.client.post(f"/kommentar/{self.run_.pk}/")
        self.run_.refresh_from_db()
        self.assertTrue(self.run_.ai_source.startswith("groq:"))

    def test_daily_limit_and_missing_key(self):
        for _ in range(20):
            AiCall.objects.create(user=self.user, ok=True)
        with mock.patch.object(ai, "chat") as chat:
            self.client.post(f"/kommentar/{self.run_.pk}/")
        chat.assert_not_called()
        self.run_.refresh_from_db()
        self.assertEqual(self.run_.ai_source, "regeln")
        self.assertIn("Tageslimit", self.run_.ai_comment["note"])
        AiCall.objects.all().delete()
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": ""}):
            self.client.post(f"/kommentar/{self.run_.pk}/")
        self.run_.refresh_from_db()
        self.assertIn("nicht eingerichtet", self.run_.ai_comment["note"])

    def test_only_own_done_runs(self):
        other = User.objects.create_user("b@b.de", "b@b.de", "pw-Bbbb-1234")
        foreign = make_run(other)
        self.assertEqual(self.client.post(f"/kommentar/{foreign.pk}/").status_code, 404)
        self.assertEqual(self.client.get(f"/kommentar/{self.run_.pk}/").status_code, 405)
