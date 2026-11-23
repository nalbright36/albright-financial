"""New Money/Meals items created from Notes default to Personal, not the last-picked space switcher.

A remembered circle (kitchen() also remembers it) used to receive @budget and @grocery items even when
the person was writing a private note. Creating from the Plans tab with an explicit space still uses
that space. A later line on the same note stays with the first item's space. A shared note does not
put the plan in a circle.
"""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from unittest import mock

from albright_labs.circles import current_space, default_create_space
from albright_labs.testing import kitchen, personal, share_with, with_apps
from albright_meals.models import GroceryItem
from albright_notes import notes, smart
from albright_notes.models import Note

from . import budget
from .models import BudgetDraft

PASSWORD = "pw-not-real-12345"


def url(name, *args):
    return reverse(f"albright_money:{name}", args=args)


class ShareDefaultTests(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"))
        self.wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam"),
                              ["money", "notes", "meals"])
        self.client.force_login(self.me)
        self.house = kitchen(self.me, self.wife, name="Albright house")
        self.mine = personal(self.me)

    def test_remembered_circle_is_still_the_current_money_view(self):
        """The switcher still remembers the last space; only creates ignore that memory."""
        self.assertEqual(current_space(self.me, "money").pk, self.house.pk)
        self.assertEqual(default_create_space(self.me, "money").pk, self.mine.pk)

    def test_note_budget_defaults_to_personal_even_if_last_space_is_a_circle(self):
        note = notes.create(self.me, "Christmas shopping", "gifts")
        plan = budget.create_draft(self.me, "Christmas Gifts 300", note=note)
        self.assertEqual(plan.space_id, self.mine.pk)
        self.assertEqual(plan.planned, Decimal("300.00"))
        self.assertFalse(BudgetDraft.objects.filter(space=self.house, name="Christmas Gifts").exists())

    def test_create_plan_without_a_space_is_personal(self):
        plan = budget.create_plan(self.me, "Weekend in Tampa")
        self.assertEqual(plan.space_id, self.mine.pk)

    def test_plans_tab_still_creates_in_the_posted_space(self):
        response = self.client.post(url("budget_plans"), {
            "action": "create", "name": "Household gifts", "template": BudgetDraft.GIFTS,
            "space": self.house.pk,
        })
        self.assertEqual(response.status_code, 302)
        plan = BudgetDraft.objects.get(name="Household gifts")
        self.assertEqual(plan.space_id, self.house.pk)

    def test_second_budget_on_the_same_note_stays_in_that_space(self):
        note = notes.create(self.me, "Christmas shopping", "gifts")
        first = budget.create_draft(self.me, "Tree 80", note=note, space=self.house)
        second = budget.create_draft(self.me, "Lights 40", note=note)
        self.assertEqual(first.space_id, self.house.pk)
        self.assertEqual(second.space_id, self.house.pk)

    def test_shared_note_does_not_put_the_plan_in_a_circle(self):
        note = notes.create(self.me, "Christmas shopping", "gifts")
        share_with(note, self.wife, permission="edit")
        plan = smart.add_budget_draft(note, self.me, "Christmas Gifts 300")
        self.assertEqual(plan.space_id, self.mine.pk)
        self.assertFalse(BudgetDraft.objects.visible_to(self.wife).filter(pk=plan.pk).exists())

    def test_note_grocery_defaults_to_personal_even_if_last_space_is_a_circle(self):
        note = notes.create(self.me, "Shop", "- milk")
        made = smart.add_groceries(self.me, ["milk"], note=note)
        item, status = made[0]
        self.assertEqual(status, "added")
        self.assertEqual(item.space_id, self.mine.pk)
        self.assertFalse(GroceryItem.objects.filter(space=self.house, name="milk").exists())

    def test_second_grocery_on_the_same_note_stays_in_that_space(self):
        note = notes.create(self.me, "Shop", "")
        first, _ = smart.add_groceries(self.me, ["milk"], note=note, space=self.house)[0]
        Note.objects.filter(pk=note.pk).update(body=f"- milk {smart.grocery_token([first.pk])}")
        note.refresh_from_db()
        second, _ = smart.add_groceries(self.me, ["eggs"], note=note)[0]
        self.assertEqual(first.space_id, self.house.pk)
        self.assertEqual(second.space_id, self.house.pk)
