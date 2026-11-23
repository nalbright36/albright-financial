"""Budget plans (Option A, evening 1): BudgetDraft is a plan with optional BudgetPlanLine rows, templates,
the Plans tab, the note chip opening the plan, and Money space permissions."""
import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from unittest import mock

from albright_labs.testing import kitchen, personal, with_apps
from albright_notes import notes, smart

from . import budget
from .models import BudgetDraft, BudgetPlanLine, Category

PASSWORD = "pw-not-real-12345"
D = Decimal


def url(name, *args):
    return reverse(f"albright_money:{name}", args=args)


class PlanBase(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"))
        self.wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam"),
                              ["money", "notes"])
        self.stranger = with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["money", "notes"])
        self.notes_only = with_apps(User.objects.create_user(username="lea", password=PASSWORD), ["notes"])
        self.client.force_login(self.me)

    def as_user(self, user):
        client = Client()
        client.force_login(user)
        return client

    def plan(self, owner=None, space=None, name="Christmas Gifts", **kw):
        owner = owner or self.me
        return budget.create_plan(owner, name, space=space or personal(owner), **kw)


class TemplateAndModelTests(PlanBase):
    def test_create_plan_from_each_template(self):
        expected = {
            BudgetDraft.BLANK: [],
            BudgetDraft.GIFTS: ["Person or event"],
            BudgetDraft.TRIP: ["Flights", "Hotel", "Food", "Activities", "Transport", "Other"],
            BudgetDraft.EVENT: ["Venue", "Food", "Drinks", "Decor", "Favors", "Other"],
            BudgetDraft.HOME: ["Materials", "Labor", "Permits", "Furniture", "Other"],
        }
        for key, names in expected.items():
            plan = self.plan(name=key, template=key)
            self.assertEqual(plan.template, key)
            self.assertEqual(list(plan.plan_lines.values_list("name", flat=True)), names)
            self.assertTrue(all(line.status == BudgetPlanLine.TO_BUY and line.planned == 0 and line.spent == 0
                                for line in plan.plan_lines.all()))

    def test_apply_template_refuses_when_a_line_has_spend(self):
        plan = self.plan(template=BudgetDraft.GIFTS)
        line = plan.plan_lines.get()
        line.spent = D("10.00")
        line.save()
        self.assertIsNone(budget.apply_template(plan, BudgetDraft.TRIP, replace=True))
        self.assertEqual(plan.plan_lines.get().name, "Person or event")

    def test_note_draft_is_a_blank_plan_with_a_cap(self):
        note = notes.create(self.me, "Christmas shopping", "gifts")
        plan = budget.create_draft(self.me, "Christmas Gifts 300", note=note)
        self.assertEqual((plan.name, plan.planned, plan.template, plan.note_id),
                         ("Christmas Gifts", D("300.00"), BudgetDraft.BLANK, note.pk))
        self.assertFalse(plan.plan_lines.exists())
        self.assertTrue(plan.is_draft)


class PlansPageTests(PlanBase):
    def test_tabs_on_this_month_and_plans(self):
        month = self.client.get(url("budget")).content.decode()
        self.assertIn("This month", month)
        self.assertIn(url("budget_plans"), month)
        self.assertRegex(month, r'space-switch__item is-on[^>]*>This month')
        plans = self.client.get(url("budget_plans")).content.decode()
        self.assertRegex(plans, r'space-switch__item is-on[^>]*>Plans')
        self.assertIn("Start blank", plans)
        self.assertIn("Gifts &amp; holiday", plans)
        self.assertIn('id="nav-back"', plans)
        self.assertIn("albright_money/money.css", plans)

    def test_create_from_template_on_the_plans_page(self):
        r = self.client.post(url("budget_plans"), {
            "action": "create", "name": "Weekend in Tampa", "template": BudgetDraft.TRIP,
            "space": personal(self.me).pk,
        })
        plan = BudgetDraft.objects.get(name="Weekend in Tampa")
        self.assertRedirects(r, budget.plan_url(plan), fetch_redirect_response=False)
        self.assertEqual(list(plan.plan_lines.values_list("name", flat=True)), budget.TEMPLATE_LINES[BudgetDraft.TRIP])
        page = self.client.get(url("budget_plan", plan.pk)).content.decode()
        self.assertIn("Flights", page)
        self.assertIn("Hotel", page)

    def test_apply_template_on_an_empty_plan(self):
        plan = self.plan(template=BudgetDraft.BLANK)
        self.client.post(url("budget_plan", plan.pk), {"action": "apply_template", "template": BudgetDraft.GIFTS})
        plan.refresh_from_db()
        self.assertEqual(plan.template, BudgetDraft.GIFTS)
        self.assertEqual(plan.plan_lines.get().name, "Person or event")


class NoteChipTests(PlanBase):
    def test_the_note_chip_opens_the_plan(self):
        note = notes.create(self.me, "Christmas shopping")
        r = self.client.post(reverse("albright_notes:budget", args=[note.pk]),
                             data=json.dumps({"text": "Christmas Gifts 300"}),
                             content_type="application/json")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        plan = BudgetDraft.objects.get(pk=data["id"])
        self.assertEqual(data["url"], budget.plan_url(plan))
        self.assertIn(f"/money/budget/plans/{plan.pk}/", data["url"])
        info = self.client.get(reverse("albright_notes:budget_info", args=[note.pk, plan.pk])).json()
        self.assertEqual(info["url"], budget.plan_url(plan))
        self.assertContains(self.client.get(url("budget")), "Open plan")
        self.assertContains(self.client.get(url("budget")), plan.name)


class PermissionTests(PlanBase):
    def test_a_stranger_cannot_see_or_edit_a_personal_plan(self):
        plan = self.plan(template=BudgetDraft.GIFTS)
        other = self.as_user(self.stranger)
        self.assertEqual(other.get(url("budget_plan", plan.pk)).status_code, 404)
        self.assertEqual(other.post(url("budget_plan", plan.pk),
                                    {"action": "apply_template", "template": BudgetDraft.TRIP}).status_code, 404)
        self.assertEqual(other.post(url("budget_plans"), {
            "action": "create", "name": "Hijack", "template": BudgetDraft.BLANK, "space": personal(self.me).pk,
        }).status_code, 404)
        self.assertFalse(BudgetDraft.objects.filter(space=personal(self.me), name="Hijack").exists())
        listing = other.get(url("budget_plans")).content.decode()
        self.assertNotIn(url("budget_plan", plan.pk), listing)
        self.assertFalse(BudgetDraft.objects.visible_to(self.stranger).filter(pk=plan.pk).exists())

    def test_a_circle_member_can_open_the_shared_plan_but_not_a_personal_one(self):
        house = kitchen(self.me, self.wife, name="The Albrights")
        shared = self.plan(space=house, name="Household gifts", template=BudgetDraft.GIFTS)
        mine = self.plan(name="My gifts", template=BudgetDraft.GIFTS)
        sam = self.as_user(self.wife)
        self.assertContains(sam.get(url("budget_plan", shared.pk)), "Household gifts")
        self.assertEqual(sam.get(url("budget_plan", mine.pk)).status_code, 404)
        self.assertEqual(sam.post(url("budget_plan", mine.pk),
                                  {"action": "apply_template", "template": BudgetDraft.TRIP}).status_code, 404)
        listing = sam.get(url("budget_plans") + f"?space={house.pk}").content.decode()
        self.assertIn("Household gifts", listing)
        self.assertNotIn("My gifts", listing)

    def test_someone_without_money_cannot_open_the_plan(self):
        plan = self.plan()
        r = self.as_user(self.notes_only).get(url("budget_plan", plan.pk))
        self.assertEqual(r.status_code, 404)

    def test_a_shared_note_does_not_open_a_plan_in_a_money_space_you_cannot_see(self):
        note = notes.create(self.me, "Christmas shopping")
        plan = budget.create_draft(self.me, "Christmas Gifts 300", note=note)
        from albright_labs.testing import share_with
        share_with(note, self.wife, permission="view", by=self.me)
        sam = self.as_user(self.wife)
        # She can open the note, but the plan is in Nate's personal Money space.
        self.assertEqual(sam.get(reverse("albright_notes:note", args=[note.pk])).status_code, 200)
        self.assertEqual(sam.get(reverse("albright_notes:budget_info", args=[note.pk, plan.pk])).status_code, 404)
        self.assertEqual(sam.get(url("budget_plan", plan.pk)).status_code, 404)
        self.assertIsNone(smart.budget_draft(self.wife, plan.pk))
