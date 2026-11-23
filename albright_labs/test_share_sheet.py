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


class ShareSheetEvening2Tests(ShareSheetTests):
    """Remaining apps and the planner quick view (evening 2). Same kitchen / people."""

    def account(self, space=None, name="Checking", owner=None):
        from albright_money.models import Account
        owner = owner or self.me
        return Account.objects.create(name=name, owner=owner, space=space or self.mine,
                                      starting_date=timezone.localdate())

    def recipe(self, space=None, title="Family chili", created_by=None):
        from albright_meals.models import Recipe
        return Recipe.objects.create(space=space or self.mine, title=title,
                                     created_by=created_by or self.me)

    def idea(self, space=None, title="Try the new bakery", added_by=None):
        from albright_meals.models import MealIdea
        return MealIdea.objects.create(space=space or self.mine, title=title,
                                       added_by=added_by or self.me)

    def test_account_page_has_a_share_button(self):
        a = self.account()
        html = self.client.get(reverse("albright_money:account", args=[a.pk])).content.decode()
        self.assertIn("data-share-sheet-open", html)
        self.assertIn("Personal", html)
        self.assertIn("Albright house", html)

    def test_owner_can_move_an_account_to_a_circle_and_back(self):
        a = self.account()
        self.assertEqual(self.post_share(a, {"action": "set_space", "target": f"space:{self.house.pk}"}).status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.space_id, self.house.pk)
        self.assertEqual(self.post_share(a, {"action": "set_space", "target": "personal"}).status_code, 302)
        a.refresh_from_db()
        self.assertEqual(a.space_id, self.mine.pk)

    def test_circle_member_cannot_move_someone_elses_account(self):
        a = self.account(space=self.house)
        sam_personal = personal(self.wife)
        self.post_share(a, {"action": "set_space", "target": "personal"}, user=self.wife)
        a.refresh_from_db()
        self.assertEqual(a.space_id, self.house.pk)
        # The account form is locked the same way.
        sam = self.as_user(self.wife)
        sam.post(reverse("albright_money:account", args=[a.pk]), {
            "name": a.name, "kind": a.kind, "starting_balance": "0.00",
            "starting_date": a.starting_date.isoformat(), "space": sam_personal.pk,
        })
        a.refresh_from_db()
        self.assertEqual(a.space_id, self.house.pk)

    def test_cannot_move_an_account_to_a_circle_the_user_is_not_in(self):
        a = self.account()
        self.post_share(a, {"action": "set_space", "target": f"space:{self.kims.pk}"})
        a.refresh_from_db()
        self.assertEqual(a.space_id, self.mine.pk)

    def test_recipe_page_has_share_radios_and_no_move_dropdown(self):
        recipe = self.recipe()
        html = self.client.get(reverse("albright_meals:recipe", args=[recipe.pk])).content.decode()
        self.assertIn("data-share-sheet-open", html)
        self.assertIn("Personal", html)
        self.assertIn("Albright house", html)
        self.assertNotIn("recipe-move", html)
        self.assertNotIn("Move to space", html)

    def test_author_can_move_a_recipe_to_a_circle_and_back(self):
        recipe = self.recipe()
        self.assertEqual(self.post_share(recipe, {"action": "set_space", "target": f"space:{self.house.pk}"}).status_code, 302)
        recipe.refresh_from_db()
        self.assertEqual(recipe.space_id, self.house.pk)
        self.post_share(recipe, {"action": "set_space", "target": "personal"})
        recipe.refresh_from_db()
        self.assertEqual(recipe.space_id, self.mine.pk)

    def test_circle_member_cannot_move_someone_elses_recipe(self):
        recipe = self.recipe(space=self.house)
        self.post_share(recipe, {"action": "set_space", "target": "personal"}, user=self.wife)
        recipe.refresh_from_db()
        self.assertEqual(recipe.space_id, self.house.pk)
        self.as_user(self.wife).post(reverse("albright_meals:recipe_move", args=[recipe.pk]),
                                     {"to": personal(self.wife).pk})
        recipe.refresh_from_db()
        self.assertEqual(recipe.space_id, self.house.pk)

    def test_cannot_move_a_recipe_to_a_circle_the_user_is_not_in(self):
        recipe = self.recipe()
        self.post_share(recipe, {"action": "set_space", "target": f"space:{self.kims.pk}"})
        recipe.refresh_from_db()
        self.assertEqual(recipe.space_id, self.mine.pk)

    def test_idea_author_can_move_and_a_member_cannot(self):
        idea = self.idea(space=self.house)
        html = self.client.get(reverse("albright_meals:ideas") + f"?space={self.house.pk}").content.decode()
        self.assertIn("data-share-sheet-open", html)
        self.assertIn(idea.title, html)
        self.post_share(idea, {"action": "set_space", "target": "personal"})
        idea.refresh_from_db()
        self.assertEqual(idea.space_id, self.mine.pk)
        idea = self.idea(space=self.house, title="Sam must not move this")
        self.post_share(idea, {"action": "set_space", "target": "personal"}, user=self.wife)
        idea.refresh_from_db()
        self.assertEqual(idea.space_id, self.house.pk)

    def test_workout_program_and_visual_pages_have_a_share_button(self):
        from albright_assistant.models import Visual
        from albright_fitness.models import Program, Workout
        w = Workout.objects.create(owner=self.me, date=timezone.localdate(), name="Push day")
        html = self.client.get(reverse("albright_fitness:workout", args=[w.pk])).content.decode()
        self.assertIn("data-share-sheet-open", html)
        self.assertIn("Personal", html)
        p = Program.objects.create(owner=self.me, name="Strength")
        html = self.client.get(reverse("albright_fitness:program", args=[p.pk])).content.decode()
        self.assertIn("data-share-sheet-open", html)
        visual = Visual.objects.create(owner=self.me, title="Spending this month", saved=True)
        html = self.client.get(reverse("assistant:visual", args=[visual.pk])).content.decode()
        self.assertIn("data-share-sheet-open", html)
        self.assertIn("Personal", html)

    def test_planner_quick_view_has_a_share_button(self):
        item = ScheduleItem.objects.create(owner=self.me, title="Buy wrapping paper", date=timezone.localdate())
        html = self.client.get(reverse("albright_planner:item", args=[item.pk]), {"panel": "1"}).content.decode()
        self.assertIn("data-share-sheet-open", html)
        self.assertIn("Personal", html)
        self.assertIn("Albright house", html)
