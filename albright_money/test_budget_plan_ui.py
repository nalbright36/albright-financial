"""Budget plans (Option A, evening 2): editing lines, the summary, over-budget warnings, and reserving in a month."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from albright_labs.testing import personal, with_apps

from . import budget, money
from .models import BudgetDraft, BudgetLine, BudgetPlanLine, Category

PASSWORD = "pw-not-real-12345"
D = Decimal


def url(name, *args):
    return reverse(f"albright_money:{name}", args=args)


class PlanUITests(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.stranger = with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["money"])
        self.client.force_login(self.me)
        money.ensure_categories(self.me, personal(self.me))
        self.plan = budget.create_plan(self.me, "Christmas Gifts", template=BudgetDraft.BLANK,
                                       planned=D("300"), space=personal(self.me),
                                       due_date=timezone.localdate() + timedelta(days=31))
        self.lexi = BudgetPlanLine.objects.create(plan=self.plan, name="Lexi", planned=D("150"), spent=D("0"), order=0)
        self.mom = BudgetPlanLine.objects.create(plan=self.plan, name="Mom", planned=D("75"), spent=D("42.18"),
                                                 notes="Sweater from Target", order=1)
        self.brutus = BudgetPlanLine.objects.create(plan=self.plan, name="Brutus", planned=D("40"), spent=D("40"),
                                                    status=BudgetPlanLine.BOUGHT, order=2)
        # 265 planned, 82.18 spent — plus a later over-budget line in one test

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def test_summary_on_the_plan_page(self):
        page = self.client.get(url("budget_plan", self.plan.pk)).content.decode()
        self.assertIn("$82.18", page)
        self.assertIn("$265.00", page)          # planned
        self.assertIn("To buy", page)
        self.assertIn("2 of 3", page)           # 1 bought, 2 to buy
        self.assertIn("due in 31 days", page)
        self.assertIn("/week", page)
        self.assertIn("Cap from the note", page)
        # lines 265 vs cap 300: not over the cap
        self.assertNotIn("that cap", page)
        self.assertIn("mn-stats", page)
        self.assertIn("mn-pline", page)

    def test_cap_over_and_line_over_warnings(self):
        BudgetPlanLine.objects.create(plan=self.plan, name="Dad", planned=D("75"), spent=D("90"), order=3)
        # planned 340 > cap 300; Dad is $15 over
        page = self.client.get(url("budget_plan", self.plan.pk)).content.decode()
        self.assertIn("that cap", page)
        self.assertIn("mn-pline--over", page)
        self.assertIn("over", page)

    def test_edit_add_and_delete_lines(self):
        self.client.post(url("budget_plan", self.plan.pk), {
            "action": "save_lines",
            f"line_{self.lexi.pk}_name": "Lexi",
            f"line_{self.lexi.pk}_planned": "160.00",
            f"line_{self.lexi.pk}_spent": "25",
            f"line_{self.lexi.pk}_status": BudgetPlanLine.TO_BUY,
            f"line_{self.lexi.pk}_notes": "Book + candle",
            f"line_{self.mom.pk}_name": "Mom",
            f"line_{self.mom.pk}_planned": "75",
            f"line_{self.mom.pk}_spent": "42.18",
            f"line_{self.mom.pk}_status": BudgetPlanLine.TO_BUY,
            f"line_{self.mom.pk}_notes": "Sweater from Target",
            f"line_{self.brutus.pk}_name": "Brutus",
            f"line_{self.brutus.pk}_planned": "40",
            f"line_{self.brutus.pk}_spent": "40",
            f"line_{self.brutus.pk}_status": BudgetPlanLine.BOUGHT,
            f"line_{self.brutus.pk}_notes": "",
        })
        self.lexi.refresh_from_db()
        self.assertEqual((self.lexi.planned, self.lexi.spent, self.lexi.notes),
                         (D("160.00"), D("25.00"), "Book + candle"))
        self.client.post(url("budget_plan", self.plan.pk), {
            "action": "add_line", "name": "Hostess gift", "planned": "30",
        })
        self.assertTrue(self.plan.plan_lines.filter(name="Hostess gift", planned=D("30.00")).exists())
        self.client.post(url("budget_plan", self.plan.pk), {
            "action": "delete_line", "line": self.brutus.pk,
        })
        self.assertFalse(self.plan.plan_lines.filter(pk=self.brutus.pk).exists())

    def test_reserve_adds_to_the_monthly_category_and_keeps_the_plan(self):
        gifts = Category.objects.get(money.scope_filter(self.me, personal(self.me)), name="Gifts")
        month = budget.this_month()
        self.client.post(url("budget_plan", self.plan.pk), {
            "action": "reserve", "category": gifts.pk, "month": f"{month:%Y-%m}", "planned": "265",
        })
        self.plan.refresh_from_db()
        self.assertTrue(self.plan.is_reserved)
        self.assertEqual(self.plan.line.category_id, gifts.pk)
        self.assertEqual(self.plan.planned, D("300"))   # cap is kept
        self.assertEqual(BudgetLine.objects.get(budget__month=month, category=gifts).planned, D("265.00"))
        page = self.client.get(url("budget_plan", self.plan.pk)).content.decode()
        self.assertIn("reserved in Gifts", page)
        # still has its lines
        self.assertEqual(self.plan.plan_lines.count(), 3)

    def test_a_stranger_cannot_edit_or_reserve(self):
        gifts = Category.objects.get(money.scope_filter(self.me, personal(self.me)), name="Gifts")
        other = self.as_user(self.stranger)
        self.assertEqual(other.post(url("budget_plan", self.plan.pk), {
            "action": "add_line", "name": "Nope", "planned": "5",
        }).status_code, 404)
        self.assertEqual(other.post(url("budget_plan", self.plan.pk), {
            "action": "reserve", "category": gifts.pk, "month": f"{budget.this_month():%Y-%m}", "planned": "265",
        }).status_code, 404)
        self.assertFalse(self.plan.plan_lines.filter(name="Nope").exists())
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.is_reserved)

    def test_phone_layout_keeps_amounts_apart_and_hides_templates_when_there_are_lines(self):
        page = self.client.get(url("budget_plan", self.plan.pk)).content.decode()
        # Planned / Spent are separate fields, not .mn-bline__input (that grid-area stacked them).
        self.assertIn("mn-pline__planned", page)
        self.assertIn("mn-pline__spent", page)
        self.assertIn("mn-pline__spent-read", page)
        self.assertIn(f'name="line_{self.lexi.pk}_planned"', page)
        self.assertNotIn(f'name="line_{self.lexi.pk}_spent"', page)
        self.assertIn("mn-pline__items", page)
        self.assertIn('name="action" value="add_item"', page)
        start = page.find(f'name="line_{self.lexi.pk}_name"')
        end = page.find("mn-pline", start + 1)
        block = page[start:end if end > start else start + 1800]
        self.assertNotIn("mn-bline__input", block)
        self.assertNotIn("Start from a template", page)
        self.assertNotIn("Change template", page)
        self.assertIn("mn-plan-meta__summary", page)
        self.assertIn("Plan details", page)
        self.assertIn("data-mn-notes", page)
        self.assertIn("data-share-sheet-open", page)
        self.assertIn("Save lines", page)
        self.assertIn('id="mn-add-name"', page)
        empty = budget.create_plan(self.me, "Empty trip", template=BudgetDraft.BLANK, space=personal(self.me))
        blank = self.client.get(url("budget_plan", empty.pk)).content.decode()
        self.assertIn("Start from a template", blank)
        self.assertIn("mn-templates", blank)
        self.assertNotIn("Save lines", blank)
