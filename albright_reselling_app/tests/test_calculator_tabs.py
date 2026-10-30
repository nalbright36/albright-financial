"""The calculator's tab order (Melt value, Buy cost, eBay profit) and its
default (Melt value) - on the full Calculators page and in the dashboard's
pop-up dialog, which share _calculator_tabs.html. Which tab was used most
recently is remembered client-side (static/js/calculator.js, localStorage);
the server always renders Melt value as the starting tab."""
import re

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from albright_labs.testing import with_apps

TAB_RE = re.compile(r'<a href="#(\w+)" class="scanner-tab( is-active)?" data-tab="\1">([^<]+)</a>')


def _tabs(content):
    nav = content.split('id="calc-tabs"')[1].split("</nav>")[0]
    return [(name, bool(active), label) for name, active, label in TAB_RE.findall(nav)]


def _panel_hidden(content, name):
    return re.search(rf'<div id="tab-{name}"( hidden)?>', content).group(1) is not None


class CalculatorTabOrderTests(TestCase):
    def setUp(self):
        self.user = with_apps(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.client.login(username="tester", password="pw-not-real-12345")

    def assert_melt_first_and_default(self, content):
        self.assertEqual(_tabs(content), [
            ("melt", True, "Melt value"), ("buy", False, "Buy cost"), ("ebay", False, "eBay profit"),
        ])
        self.assertFalse(_panel_hidden(content, "melt"))
        self.assertTrue(_panel_hidden(content, "buy"))
        self.assertTrue(_panel_hidden(content, "ebay"))

    def test_calculators_page_opens_on_melt_value(self):
        content = self.client.get(reverse("albright_reselling_app:calculator")).content.decode()

        self.assert_melt_first_and_default(content)

    def test_dialog_opens_on_melt_value(self):
        content = self.client.get(reverse("albright_reselling_app:dashboard")).content.decode()

        self.assert_melt_first_and_default(content)

    def test_no_js_post_keeps_its_own_tab(self):
        for tab in ("buy", "ebay", "melt"):
            with self.subTest(tab=tab):
                content = self.client.post(reverse("albright_reselling_app:calculator"), {"tab": tab}).content.decode()
                active = [name for name, is_active, _label in _tabs(content) if is_active]
                self.assertEqual(active, [tab])
                self.assertFalse(_panel_hidden(content, tab))

    def test_post_with_no_or_unknown_tab_falls_back_to_melt(self):
        for data in ({}, {"tab": "bogus"}):
            with self.subTest(data=data):
                content = self.client.post(reverse("albright_reselling_app:calculator"), data).content.decode()
                self.assertEqual([name for name, is_active, _label in _tabs(content) if is_active], ["melt"])
