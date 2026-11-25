"""Notes @budget lines: creating a draft and adding / grouping / deleting items over JSON."""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from unittest import mock

from albright_labs.testing import with_apps
from albright_money.models import BudgetDraft, BudgetPlanLine
from albright_notes import notes, smart

D = Decimal
PASSWORD = "pw-not-real-12345"


class BudgetLineApiTests(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.stranger = with_apps(User.objects.create_user(username="kim", password=PASSWORD), ["notes", "money"])
        self.client.force_login(self.me)
        self.note = notes.create(self.me, "Holiday planning", "Gift list for the family.")

    def post(self, name, *args, **data):
        return self.client.post(reverse(f"albright_notes:{name}", args=args),
                                data=json.dumps(data), content_type="application/json")

    def get(self, name, *args):
        return self.client.get(reverse(f"albright_notes:{name}", args=args))

    def draft(self, text="Christmas Gifts"):
        r = self.post("budget", self.note.pk, text=text)
        self.assertEqual(r.status_code, 200, r.content)
        return r.json()

    def test_create_draft_then_add_grouped_items(self):
        data = self.draft()
        plan = BudgetDraft.objects.get(pk=data["id"])
        self.assertEqual(plan.name, "Christmas Gifts")
        self.assertFalse(plan.plan_lines.exists())
        r = self.post("budget_lines", self.note.pk, plan.pk, text="Mom 50")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["spent"], "50.00")
        self.assertEqual(body["count"], 1)
        self.assertEqual(body["groups"][0]["name"], "Mom")
        r = self.post("budget_lines", self.note.pk, plan.pk, text="Mom: scarf 30")
        body = r.json()
        self.assertEqual(body["spent"], "80.00")
        self.assertEqual(body["item_count"], 2)
        self.assertEqual(body["groups"][0]["item_count"], 2)
        self.assertEqual([i["label"] for i in body["groups"][0]["items"]], ["", "scarf"])
        info = self.get("budget_info", self.note.pk, plan.pk).json()
        self.assertEqual(info["spent"], "80.00")
        self.assertEqual(info["url"], body["url"])

    def test_empty_or_nameless_text_is_rejected(self):
        data = self.draft()
        r = self.post("budget_lines", self.note.pk, data["id"], text="50")
        self.assertEqual(r.status_code, 400)
        r = self.post("budget_lines", self.note.pk, data["id"], text="")
        self.assertEqual(r.status_code, 400)

    def test_update_and_delete_item_and_group(self):
        data = self.draft()
        self.post("budget_lines", self.note.pk, data["id"], text="Mom 50")
        body = self.post("budget_lines", self.note.pk, data["id"], text="Mom: scarf 30").json()
        item = next(i for i in body["groups"][0]["items"] if i["label"] == "scarf")
        r = self.post("budget_item", self.note.pk, data["id"], item["id"], amount="35", label="wool scarf")
        self.assertEqual(r.status_code, 200)
        updated = r.json()["groups"][0]
        self.assertEqual(updated["spent"], "85.00")
        self.assertEqual(next(i["label"] for i in updated["items"] if i["id"] == item["id"]), "wool scarf")
        r = self.post("budget_item", self.note.pk, data["id"], item["id"], delete=True)
        self.assertEqual(r.json()["spent"], "50.00")
        group_id = r.json()["groups"][0]["id"]
        r = self.post("budget_line", self.note.pk, data["id"], group_id, delete=True)
        self.assertEqual(r.json()["count"], 0)
        self.assertFalse(BudgetPlanLine.objects.filter(plan_id=data["id"]).exists())

    def test_a_stranger_cannot_add_items(self):
        data = self.draft()
        other = Client()
        other.force_login(self.stranger)
        r = other.post(reverse("albright_notes:budget_lines", args=[self.note.pk, data["id"]]),
                       data=json.dumps({"text": "Mom 50"}), content_type="application/json")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(BudgetPlanLine.objects.filter(plan_id=data["id"]).exists())
