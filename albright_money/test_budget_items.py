"""Budget plan items: one person or event can have several gifts, Spent is their sum, Planned tracks
Spent until it is set to a different cap."""
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from unittest import mock

from albright_labs.testing import personal, with_apps

from . import budget
from .models import BudgetDraft, BudgetPlanItem, BudgetPlanLine
from .test_budget_plans import PlanBase

D = Decimal


def url(name, *args):
    return reverse(f"albright_money:{name}", args=args)


class ParseAndGroupTests(PlanBase):
    def test_parse_line_text(self):
        self.assertEqual(budget.parse_line_text("Mom 50"), ("Mom", D("50.00"), ""))
        self.assertEqual(budget.parse_line_text("Mom $50"), ("Mom", D("50.00"), ""))
        self.assertEqual(budget.parse_line_text("$50 Mom"), ("Mom", D("50.00"), ""))
        self.assertEqual(budget.parse_line_text("Mom: scarf 30"), ("Mom", D("30.00"), "scarf"))
        self.assertEqual(budget.parse_line_text("Mom: wool scarf $30"), ("Mom", D("30.00"), "wool scarf"))
        self.assertIsNone(budget.parse_line_text("Mom"))
        self.assertIsNone(budget.parse_line_text("50"))
        self.assertIsNone(budget.parse_line_text(""))

    def test_same_name_adds_a_second_item_and_tracks_planned(self):
        plan = self.plan(template=BudgetDraft.BLANK)
        line, first = budget.add_item_from_text(plan, "Mom 50")
        self.assertEqual((line.name, first.amount, first.label), ("Mom", D("50.00"), ""))
        self.assertEqual((line.planned, line.spent), (D("50.00"), D("50.00")))
        same, second = budget.add_item_from_text(plan, "Mom: scarf 30")
        self.assertEqual(same.pk, line.pk)
        line.refresh_from_db()
        self.assertEqual(line.items.count(), 2)
        self.assertEqual((second.label, second.amount), ("scarf", D("30.00")))
        self.assertEqual((line.planned, line.spent), (D("80.00"), D("80.00")))
        # case-insensitive match
        budget.add_item_from_text(plan, "mom 10")
        line.refresh_from_db()
        self.assertEqual(plan.plan_lines.count(), 1)
        self.assertEqual((line.planned, line.spent), (D("90.00"), D("90.00")))

    def test_planned_stops_tracking_once_it_is_set_apart(self):
        plan = self.plan(template=BudgetDraft.BLANK)
        line, _ = budget.add_item_from_text(plan, "Mom 50")
        budget.add_item_from_text(plan, "Mom 30")
        line.refresh_from_db()
        line.planned = D("100.00")
        line.planned_auto = False
        line.save(update_fields=["planned", "planned_auto"])
        budget.add_item_from_text(plan, "Mom: socks 20")
        line.refresh_from_db()
        self.assertEqual(line.spent, D("100.00"))
        self.assertEqual(line.planned, D("100.00"))  # stayed at the cap
        budget.add_item_from_text(plan, "Mom 5")
        line.refresh_from_db()
        self.assertEqual(line.spent, D("105.00"))
        self.assertEqual(line.planned, D("100.00"))

    def test_deleting_an_item_resyncs_spent_and_tracked_planned(self):
        plan = self.plan(template=BudgetDraft.BLANK)
        line, first = budget.add_item_from_text(plan, "Mom 50")
        _, second = budget.add_item_from_text(plan, "Mom 30")
        budget.delete_item(second)
        line.refresh_from_db()
        self.assertEqual((line.planned, line.spent), (D("50.00"), D("50.00")))
        budget.delete_item(first)
        line.refresh_from_db()
        self.assertEqual((line.planned, line.spent), (D("0.00"), D("0.00")))
        self.assertFalse(line.items.exists())

    def test_existing_spent_without_items_still_counts_and_migrates_via_set_spent(self):
        plan = self.plan(template=BudgetDraft.BLANK)
        line = BudgetPlanLine.objects.create(plan=plan, name="Dad", planned=D("75"), spent=D("40"))
        totals = budget.plan_totals(plan)
        self.assertEqual(totals["spent"], D("40.00"))
        self.assertEqual(totals["rows"][0]["spent"], D("40.00"))
        budget.set_spent(line, D("42.18"))
        line.refresh_from_db()
        self.assertEqual(line.spent, D("42.18"))
        self.assertEqual(line.items.get().amount, D("42.18"))

    def test_apply_template_refuses_when_a_line_has_items(self):
        plan = self.plan(template=BudgetDraft.GIFTS)
        budget.add_item_from_text(plan, "Person or event 10")
        self.assertIsNone(budget.apply_template(plan, BudgetDraft.TRIP, replace=True))
        self.assertEqual(plan.plan_lines.get().name, "Person or event")


class PlanItemPageTests(PlanBase):
    def test_add_and_delete_item_on_the_plan_page(self):
        plan = self.plan(template=BudgetDraft.BLANK)
        line = BudgetPlanLine.objects.create(plan=plan, name="Mom", planned=D("100"), spent=D("0"), planned_auto=False, order=0)
        self.client.post(url("budget_plan", plan.pk), {
            "action": "add_item", "line": line.pk, "label": "scarf", "amount": "30",
        })
        line.refresh_from_db()
        self.assertEqual(line.spent, D("30.00"))
        # Planned was 100, not equal to previous spent 0, so it stays
        self.assertEqual(line.planned, D("100.00"))
        item = line.items.get()
        self.assertEqual((item.label, item.amount), ("scarf", D("30.00")))
        page = self.client.get(url("budget_plan", plan.pk)).content.decode()
        self.assertIn("$30.00", page)
        self.assertIn("of $100.00 planned", page)
        self.assertIn("scarf", page)
        self.assertIn("mn-pline__spent-read", page)
        self.client.post(url("budget_plan", plan.pk), {"action": "delete_item", "item": item.pk})
        line.refresh_from_db()
        self.assertEqual(line.spent, D("0.00"))
        self.assertFalse(line.items.exists())
        # Planned stays at the cap after a delete when it was not tracking
        self.assertEqual(line.planned, D("100.00"))

    def test_payload_lists_groups_and_items(self):
        plan = self.plan(template=BudgetDraft.BLANK)
        budget.add_item_from_text(plan, "Mom 50")
        budget.add_item_from_text(plan, "Mom: scarf 30")
        data = budget.plan_payload(plan)
        self.assertEqual(data["spent"], "80.00")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["item_count"], 2)
        mom = data["groups"][0]
        self.assertEqual(mom["name"], "Mom")
        self.assertEqual(mom["spent"], "80.00")
        self.assertEqual([i["label"] for i in mom["items"]], ["", "scarf"])
