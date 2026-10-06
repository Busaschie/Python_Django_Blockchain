"""Tests: Jedes Formularfeld hat eine Erklärung (i-Symbol), Texte sind vollständig und werden korrekt ausgeliefert."""
import re

from django.test import TestCase

from .forms import HELP, PARAM_TIPS, SIZE_TIPS, UI_TIPS, BacktestForm
from .strategies import STRATEGIES


class TooltipContentTests(TestCase):
    def test_every_form_field_except_chain_has_an_explanation(self):
        names = set(BacktestForm().fields) - {"chain"}
        self.assertEqual(names - set(HELP), set(), "Feld ohne Erklärung")
        self.assertEqual(set(HELP) - names, set(), "Erklärung ohne Feld")
        for name, text in HELP.items():
            self.assertGreater(len(text), 60, name)
            self.assertLess(len(text), 420, name)
            self.assertNotIn('"', text, name)  # würde das HTML-Attribut stören

    def test_form_sets_help_text(self):
        form = BacktestForm()
        for name in HELP:
            self.assertEqual(form.fields[name].help_text, HELP[name])

    def test_parameter_tips_exist_for_every_strategy(self):
        self.assertEqual(set(PARAM_TIPS), set(STRATEGIES))
        for strategy, tips in PARAM_TIPS.items():
            self.assertEqual(len(tips), 3)
            self.assertTrue(tips[0] and tips[1], strategy)  # Parameter 1 und 2 immer erklärt
        self.assertTrue(PARAM_TIPS["rsi"][2])  # RSI hat einen dritten Parameter
        self.assertEqual(set(SIZE_TIPS), {"fixed", "vol"})

    def test_all_ui_tips_present(self):
        self.assertEqual(set(UI_TIPS), {"chain", "run", "compare_chains", "compare_strategies", "montecarlo", "plausibility", "regimes", "costs"})


class TooltipRenderTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_user("t@t.de", "t@t.de", "x"))
        self.html = self.client.get("/").content.decode()

    def test_every_field_has_an_info_icon_with_text(self):
        for name in set(BacktestForm().fields) - {"chain"}:
            block = re.search(rf'<div class="field" data-field="{name}">(.*?)</div>\s*(?:<select|<input)', self.html, re.S)
            self.assertIsNotNone(block, f"Feldblock {name} fehlt")
            self.assertIn('class="tip"', block.group(1), name)
            self.assertIn("data-tip=", block.group(1), name)
            self.assertIn("aria-label=", block.group(1), name)

    def test_chain_selection_and_buttons_have_tips(self):
        self.assertEqual(self.html.count('aria-label="Erklärung: Blockchain"'), 1)
        for key in ("run", "compare_chains", "compare_strategies"):
            self.assertIn(f'data-tip="{UI_TIPS[key]}"', self.html.replace("&quot;", '"'))
        self.assertNotIn('title="Gleiche Einstellungen', self.html)  # kein doppelter nativer Tooltip

    def test_tip_data_and_script_are_delivered(self):
        self.assertIn('id="tipdata"', self.html)
        self.assertIn('bubble.setAttribute("role", "tooltip")', self.html)
        data = re.search(r'<script id="tipdata" type="application/json">(.*?)</script>', self.html, re.S).group(1)
        self.assertIn("sma_cross", data)
        self.assertIn('"vol"', data)

    def test_icons_are_buttons_that_do_not_submit(self):
        icons = re.findall(r'<button type="([a-z]+)" class="tip"', self.html)
        self.assertGreaterEqual(len(icons), len(HELP) + 1)
        self.assertEqual(set(icons), {"button"})

    def test_text_is_html_escaped_and_attributes_are_well_formed(self):
        from html.parser import HTMLParser

        class Collect(HTMLParser):
            def __init__(self):
                super().__init__()
                self.tips = []

            def handle_starttag(self, tag, attrs):
                a = dict(attrs)
                if tag == "button" and "tip" in (a.get("class") or "").split():
                    self.tips.append(a)

        c = Collect()
        c.feed(self.html)
        self.assertEqual(len(c.tips), len(HELP) + 1)  # alle Felder + Blockchain-Auswahl
        for a in c.tips:
            self.assertTrue(a.get("data-tip") and len(a["data-tip"]) > 40, a)
            self.assertTrue(a.get("aria-label", "").startswith("Erklärung: "), a)
        self.assertIn("„Strategien vergleichen“", self.html)

    def test_aria_describedby_targets_exist(self):
        for name in HELP:
            self.assertIn(f'id="id_{name}_helptext"', self.html, name)  # Ziel von aria-describedby
