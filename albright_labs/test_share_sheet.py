"""Share sheet (Option A, evening 1): Personal / circle radios, owner-only changes, and the
Share button on Money plans, Notes, and the Planner item page.

A circle member must not be able to move someone else's plan into their Personal space, or
into a circle they aren't in. The item is left alone and the POST is rejected.
"""
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from albright_labs.testing import kitchen, personal, share_with, with_apps
from albright_money import budget
from albright_money.models import BudgetDraft
from albright_notes import notes
from albright_planner.models import ScheduleItem

from . import sharing
from .models import ShareGrant, Space

PASSWORD = "pw-not-real-12345"


def share_url(obj):
    kind = sharing.kind_of(obj)
    if kind is not None:
        return reverse("labs:share", args=[kind.label, obj.pk])
    return reverse("labs:share", args=[obj._meta.label, obj.pk])


class ShareSheetTests(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"))
        self.wife = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam"))
        self.stranger = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim"))
        self.house = kitchen(self.me, self.wife, name="Albright house",
                          apps=("meals", "money", "notes", "planner", "fitness"))
        self.kims = kitchen(self.stranger, name="Kim's house",
                         apps=("meals", "money", "notes", "planner", "fitness"))
        self.mine = personal(self.me)
        self.client.force_login(self.me)

    def as_user(self, user):
        client = Client()
        client.force_login(user)
        return client

    def plan(self, space=None, name="Christmas Gifts", owner=None):
        return budget.create_plan(owner or self.me, name, space=space or self.mine)

    def post_share(self, obj, data, user=None):
        client = self.as_user(user) if user is not None else self.client
        return client.post(share_url(obj), data)

    def test_plan_page_has_a_share_button_and_personal_is_on(self):
        plan = self.plan()
        html = self.client.get(budget.plan_url(plan)).content.decode()
        self.assertIn("Share", html)
        self.assertIn('data-share-sheet-open', html)
        self.assertIn("Personal", html)
        self.assertIn("Albright house", html)
        # Personal is the selected radio (the plan lives there).
        self.assertRegex(html, r'share-choice is-on[^>]*>[\s\S]*?Personal')

    def test_owner_can_move_a_plan_to_a_circle_they_are_in(self):
        plan = self.plan()
        response = self.post_share(plan, {"action": "set_space", "target": f"space:{self.house.pk}"})
        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual(plan.space_id, self.house.pk)

    def test_owner_can_move_a_plan_back_to_personal(self):
        plan = self.plan(space=self.house)
        response = self.post_share(plan, {"action": "set_space", "target": "personal"})
        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual(plan.space_id, self.mine.pk)

    def test_circle_member_cannot_move_someone_elses_plan(self):
        plan = self.plan(space=self.house)
        # Sam can see the plan (shared Money space) but must not move it to her Personal.
        sam_personal = personal(self.wife)
        response = self.post_share(plan, {"action": "set_space", "target": "personal"}, user=self.wife)
        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual(plan.space_id, self.house.pk)
        # The error is in messages; the plan is untouched.
        self.assertFalse(BudgetDraft.objects.filter(pk=plan.pk, space=sam_personal).exists())

    def test_cannot_move_a_plan_to_a_circle_the_user_is_not_in(self):
        plan = self.plan()
        response = self.post_share(plan, {"action": "set_space", "target": f"space:{self.kims.pk}"})
        self.assertEqual(response.status_code, 302)
        plan.refresh_from_db()
        self.assertEqual(plan.space_id, self.mine.pk)

    def test_stranger_cannot_see_or_share_a_personal_plan(self):
        plan = self.plan()
        kim = self.as_user(self.stranger)
        self.assertEqual(kim.get(budget.plan_url(plan)).status_code, 404)
        self.assertEqual(kim.post(share_url(plan), {"action": "set_space", "target": "personal"}).status_code, 404)
        plan.refresh_from_db()
        self.assertEqual(plan.space_id, self.mine.pk)

    def test_notes_sheet_has_personal_and_circle_radios(self):
        note = notes.create(self.me, "Christmas shopping", "gifts")
        html = self.client.get(reverse("albright_notes:note", args=[note.pk])).content.decode()
        self.assertIn('data-pop="nt-pop-share"', html)
        self.assertIn("Personal", html)
        self.assertIn("Albright house", html)

    def test_owner_can_open_a_note_to_a_circle_and_back_to_personal(self):
        note = notes.create(self.me, "Christmas shopping", "gifts")
        response = self.post_share(note, {"action": "set_visibility", "target": f"circle:{self.house.circle_id}"})
        self.assertEqual(response.status_code, 302)
        grants = list(sharing.grants(note))
        self.assertEqual(len(grants), 1)
        self.assertEqual(grants[0].circle_id, self.house.circle_id)
        # The wife can now open the note.
        self.assertEqual(self.as_user(self.wife).get(reverse("albright_notes:note", args=[note.pk])).status_code, 200)
        self.post_share(note, {"action": "set_visibility", "target": "personal"})
        self.assertFalse(sharing.grants(note))
        from albright_notes.models import Note
        self.assertFalse(Note.objects.visible_to(self.wife).filter(pk=note.pk).exists())
        # item_page sends a browser GET to the app home (302) instead of a 404 page.
        self.assertEqual(self.as_user(self.wife).get(reverse("albright_notes:note", args=[note.pk])).status_code, 302)

    def test_cannot_share_a_note_with_a_circle_the_user_is_not_in(self):
        note = notes.create(self.me, "Private list", "")
        response = self.post_share(note, {"action": "set_visibility", "target": f"circle:{self.kims.circle_id}"})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(sharing.grants(note))

    def test_non_owner_cannot_change_a_notes_visibility(self):
        note = notes.create(self.me, "Christmas shopping", "gifts")
        share_with(note, self.wife, permission="edit")
        # Sam can open it, but must not open it to the house or flip it back to only Nate.
        response = self.post_share(note, {"action": "set_visibility", "target": f"circle:{self.house.circle_id}"},
                                   user=self.wife)
        self.assertEqual(response.status_code, 302)
        self.assertFalse(any(g.circle_id for g in sharing.grants(note)))

    def test_people_grants_stay_when_visibility_goes_back_to_personal(self):
        note = notes.create(self.me, "Christmas shopping", "gifts")
        share_with(note, self.wife, permission="edit")
        self.post_share(note, {"action": "set_visibility", "target": f"circle:{self.house.circle_id}"})
        self.post_share(note, {"action": "set_visibility", "target": "personal"})
        grants = list(sharing.grants(note))
        self.assertEqual(len(grants), 1)
        self.assertEqual((grants[0].user_id, grants[0].permission), (self.wife.pk, "edit"))

    def test_planner_item_page_has_a_share_button(self):
        item = ScheduleItem.objects.create(owner=self.me, title="Buy wrapping paper", date=timezone.localdate())
        html = self.client.get(reverse("albright_planner:item", args=[item.pk])).content.decode()
        self.assertIn("Share", html)
        self.assertIn("data-share-sheet-open", html)
        self.assertIn("Personal", html)
        self.assertIn("Albright house", html)
