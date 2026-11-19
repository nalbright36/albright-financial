"""The floating dock (templates/index.html .fab-dock): a page's floating buttons and the AI assistant's in one fixed row,
the AI button always last - so Meals' Convert can never sit under it again. Only the dock is position: fixed."""
import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.test import TestCase
from django.urls import reverse

from albright_labs.models import UserPreference
from albright_labs.testing import kitchen, with_apps

from .models import Recipe

PASSWORD = "pw-not-real-12345"
DOCK = '<div class="fab-dock" id="fab-dock">'


def dock(page):
    """The dock's inner HTML ("" if it's empty)."""
    assert page.count(DOCK) == 1, "exactly one dock"
    return page.split(DOCK, 1)[1].split("</div>", 1)[0]


def dock_ids(page):
    return re.findall(r'<button[^>]*\bid="([^"]+)"', dock(page))


class DockTests(TestCase):
    def setUp(self):
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD), ["meals", "notes"])
        self.space = kitchen(self.me)
        self.recipe = Recipe.objects.create(space=self.space, title="Cookies", created_by=self.me, ingredients=[
            {"name": "butter", "quantity": "1", "unit": "cup", "note": ""}])
        self.client.force_login(self.me)

    def allow_assistant(self, on=True):
        UserPreference.objects.filter(user=self.me).update(assistant_allowed=on)

    def recipe_page(self):
        return self.client.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).content.decode()

    def test_permitted_user_gets_converter_then_ai_button(self):
        self.allow_assistant()
        page = self.recipe_page()
        self.assertEqual(dock_ids(page), ["meals-convert-open", "assistant-fab"])     # AI rightmost
        self.assertIn('aria-label="Measurement converter"', dock(page))
        self.assertIn('id="assistant"', page)                          # its drawer / connect card, outside the dock
        self.assertNotIn('id="assistant"', dock(page))
        self.assertIn("assistant/assistant.js", page)
        # The add/edit page has the converter in the dock too.
        page = self.client.get(reverse("albright_meals:recipe_edit", args=[self.recipe.pk])).content.decode()
        self.assertEqual(dock_ids(page), ["meals-convert-open", "assistant-fab"])

    def test_user_without_permission_gets_only_the_converter(self):
        page = self.recipe_page()
        self.assertEqual(dock_ids(page), ["meals-convert-open"])
        for absent in ('id="assistant-fab"', 'id="assistant"', "assistant/assistant.js", "assistant/assistant.css",
                       'class="as-'):
            self.assertNotIn(absent, page)

    def test_no_dock_items_elsewhere_without_permission(self):
        page = self.client.get(reverse("albright_notes:list")).content.decode()
        self.assertEqual(dock(page), "")              # an empty element - .fab-dock:empty is display: none
        css = Path(finders.find("style.css")).read_text(encoding="utf-8")
        self.assertRegex(css, r"\.fab-dock:empty\s*\{\s*display:\s*none;\s*\}")

    def test_permitted_user_gets_just_the_ai_button_elsewhere(self):
        self.allow_assistant()
        page = self.client.get(reverse("albright_notes:list")).content.decode()
        self.assertEqual(dock_ids(page), ["assistant-fab"])

    def test_only_the_dock_is_position_fixed(self):
        sheets = [Path(finders.find("style.css")).read_text(encoding="utf-8"),
                  Path(finders.find("assistant/assistant.css")).read_text(encoding="utf-8")]
        rules = [(sel.strip(), body) for css in sheets
                 for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", re.sub(r"/\*.*?\*/", "", css, flags=re.S))]
        for item in (".meals-convert-fab", ".as-fab"):
            mine = [(sel, body) for sel, body in rules if item in sel]
            self.assertTrue(mine, item)
            for sel, body in mine:
                self.assertNotRegex(body, r"position:\s*fixed", sel)
                self.assertNotRegex(body, r"(^|;|\s)(right|bottom):", sel)
        dock_rule = next(body for sel, body in rules if sel == ".fab-dock")
        self.assertIn("position: fixed", dock_rule)
        self.assertIn("env(safe-area-inset-bottom", dock_rule)
        self.assertIn("flex-direction: row", dock_rule)
