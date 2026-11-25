"""The global search (albright_labs.search_views over albright_notes.links.find): it never shows someone else's private
things or anything from an app the person can't open, and it does show what's shared with them through a circle."""
from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from albright_control.models import Idea
from albright_meals.models import Recipe
from albright_money.models import Account, Transaction
from albright_notes.models import Note
from albright_planner.models import ScheduleItem, TaskChecklistItem

from . import circles
from .testing import personal, share_with, with_apps


class SearchPermissionTests(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.nate = with_apps(User.objects.create_user("nate", password="pw-not-real-1", first_name="Nathan"),
                              ["planner", "notes", "meals", "fitness"])            # no Money
        self.sam = with_apps(User.objects.create_user("sam", password="pw-not-real-2", first_name="Sam"),
                             ["planner", "notes", "meals", "fitness", "money"])
        self.kim = with_apps(User.objects.create_user("kim", password="pw-not-real-3", first_name="Kim"),
                             ["planner", "notes"])                                  # no Meals, no circle

    def titles(self, user, q="tile", app=""):
        client = Client()
        client.force_login(user)
        reply = client.get(reverse("labs:search"), {"q": q, "app": app})
        self.assertEqual(reply.status_code, 200)
        return {r["title"] for g in reply.json()["groups"] for r in g["results"]}

    def test_only_what_the_person_could_open_and_circle_shares(self):
        today = timezone.localdate()
        home = circles.create(self.sam, "Home", ["planner", "notes", "meals", "money"])
        invite, _raw, _s, _r = circles.invite(self.sam, home, "nate")
        circles.accept(self.nate, invite, ["planner", "notes", "meals", "money"])

        # Sam's private things, and the ones Sam shares with the circle.
        Note.objects.create(owner=self.sam, title="Private tile quote")
        ScheduleItem.objects.create(owner=self.sam, title="Private tile errand", date=today)
        shared_note = Note.objects.create(owner=self.sam, title="Kitchen remodel", body="call the tile guy Thursday")
        shared_task = ScheduleItem.objects.create(owner=self.sam, title="Call tile guy about backsplash", date=today)
        TaskChecklistItem.objects.create(task=shared_task, text="Pick tile samples")
        share_with(shared_note, circle=home)
        share_with(shared_task, circle=home)
        # Apps Nate can't open: Money (his own transaction, and one in the circle's space) and Admin > Ideas.
        mine = Account.objects.create(owner=self.nate, space=personal(self.nate), name="Mine", starting_date=date(2026, 1, 1))
        Transaction.objects.create(account=mine, date=today, amount=Decimal("-212.40"), payee="Tampa Tile Supply")
        theirs = Account.objects.create(owner=self.sam, space=circles.visible_spaces(self.sam, "money")[1], name="Joint",
                                        starting_date=date(2026, 1, 1))
        Transaction.objects.create(account=theirs, date=today, amount=Decimal("-40"), payee="Tile Outlet")
        Idea.objects.create(title="Tile layout planner")
        # A recipe in Kim's own space - but Kim can't open Meals.
        Recipe.objects.create(space=personal(self.kim), title="Tile-shaped crackers", created_by=self.kim)

        nate = self.titles(self.nate)
        self.assertEqual(nate, {"Kitchen remodel", "Call tile guy about backsplash", "Pick tile samples"})
        self.assertEqual(self.titles(self.nate, "212.40"), set())                   # not even by amount
        self.assertEqual(self.titles(self.nate, "tile", app="money"), nate)         # app=money: ignored, nothing leaks
        self.assertIn("Sam", self.titles(self.nate, "sam"))                         # People: in a circle together

        sam = self.titles(self.sam)
        self.assertTrue({"Private tile quote", "Private tile errand", "Tile Outlet"} <= sam)
        self.assertFalse({"Tampa Tile Supply", "Tile layout planner"} & sam)        # Nate's Personal money; no Admin

        self.assertEqual(self.titles(self.kim), set())                              # no circle, no Meals
        self.assertEqual(self.titles(self.kim, "sam"), set())

        # Leaving the circle takes the shared things out of Nate's search at once.
        circles.leave(self.nate, home)
        self.assertEqual(self.titles(self.nate), set())
