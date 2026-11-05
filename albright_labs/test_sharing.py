"""The one Share system (albright_labs.sharing): can_view / can_edit for the owner, a person, a circle (only active
members who opted in to the app) and anyone else; leaving a circle or opting out removes access in all four apps and
the Bot API at once; View vs Edit; only owners reshare or delete; sharing defaults on create, quick add and bot
create; the Share control on each app's view, edit and new screens; Fitness's diary and body data never shareable;
the Shared with me hub - and the migration keeping exactly who sees what."""
import json
from datetime import date, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotToken
from albright_fitness.models import Workout
from albright_meals.models import Recipe
from albright_notes.models import Note
from albright_planner.models import ScheduleItem

from . import circles, sharing
from .models import ShareGrant, SharingDefault, Space
from .testing import allow_api_keys, grants_of, kitchen, personal, share_with, with_apps

PASSWORD = "pw-not-real-12345"
API = "/api/bot/v1/"
APPS = ["planner", "notes", "meals", "fitness"]


@override_settings(BOT_API_REQUIRE_HTTPS=False, USDA_API_KEY="")
class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.nate = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nathan",
                                                       email="nate@example.com"), APPS)
        self.sam = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam",
                                                      email="sam@example.com"), APPS)
        self.kim = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim"), APPS)
        self.client.force_login(self.nate)
        self.today = timezone.localdate()

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def family(self, sam_apps=APPS):
        """A circle sharing all four apps; Sam joined it with sam_apps."""
        c = circles.create(self.nate, "Albright Family", APPS)
        invite, _raw, _s, _r = circles.invite(self.nate, c, "sam")
        circles.accept(self.sam, invite, list(sam_apps))
        return c

    def things(self):
        """One of Nate's things in each app."""
        return {"planner": ScheduleItem.objects.create(owner=self.nate, title="Trash night", date=self.today),
                "notes": Note.objects.create(owner=self.nate, title="Packing list"),
                "meals": Recipe.objects.create(space=personal(self.nate), title="Nate's chili", created_by=self.nate),
                "fitness": Workout.objects.create(owner=self.nate, date=self.today, name="Leg day", finished_at=timezone.now())}

    def page_of(self, app, obj):
        return {"planner": reverse("albright_planner:item", args=[obj.pk]), "notes": reverse("albright_notes:note", args=[obj.pk]),
                "meals": reverse("albright_meals:recipe", args=[obj.pk]), "fitness": reverse("albright_fitness:workout", args=[obj.pk])}[app]


# ---------------------------------------------------------------------------
# can_view / can_edit
# ---------------------------------------------------------------------------

class PermissionTests(Base):
    def test_owner_person_and_strangers(self):
        for app, obj in self.things().items():
            with self.subTest(app=app):
                self.assertTrue(sharing.can_edit(self.nate, obj))
                self.assertFalse(sharing.can_view(self.sam, obj))
                self.assertEqual(self.as_user(self.sam).get(self.page_of(app, obj)).status_code, 404)
                share_with(obj, self.sam, "view")
                self.assertTrue(sharing.can_view(self.sam, obj))
                self.assertFalse(sharing.can_edit(self.sam, obj))
                self.assertEqual(self.as_user(self.sam).get(self.page_of(app, obj)).status_code, 200)
                share_with(obj, self.sam, "edit")
                self.assertTrue(sharing.can_edit(self.sam, obj))
                self.assertFalse(sharing.can_view(self.kim, obj))
                self.assertEqual(self.as_user(self.kim).get(self.page_of(app, obj)).status_code, 404)

    def test_a_circle_counts_only_for_active_members_who_opted_in(self):
        c = self.family(sam_apps=["planner"])                      # Sam joined for the Planner only
        things = self.things()
        for app, obj in things.items():
            share_with(obj, circle=c, permission="view")
        self.assertTrue(sharing.can_view(self.sam, things["planner"]))
        for app in ("notes", "meals", "fitness"):
            self.assertFalse(sharing.can_view(self.sam, things[app]), app)
        invited, _raw, _s, _r = circles.invite(self.nate, c, "kim")   # invited, not accepted
        self.assertFalse(sharing.can_view(self.kim, things["planner"]))
        circles.set_enabled_apps(self.nate, c, ["notes"])             # the circle stops sharing the Planner
        self.assertFalse(sharing.can_view(self.sam, things["planner"]))

    def test_needs_the_app_on_the_site(self):
        obj = self.things()["notes"]
        share_with(obj, self.sam, "edit")
        with_apps(self.sam, ["planner"])
        self.assertFalse(sharing.can_view(self.sam, obj))

    def test_leaving_or_opting_out_removes_access_everywhere_and_in_the_bot_api(self):
        c = self.family()
        things = self.things()
        for obj in things.values():
            share_with(obj, circle=c, permission="edit")
        allow_api_keys(self.sam)
        _t, raw = BotToken.issue(self.sam, "bot", ["schedule:read", "notes:write", "meals:read"])
        auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        sam = self.as_user(self.sam)
        for app, obj in things.items():
            self.assertEqual(sam.get(self.page_of(app, obj)).status_code, 200, app)
        self.assertIn("Trash night", Client().get(API + f"schedule?start={self.today}&end={self.today}", **auth).content.decode())
        circles.set_participation(self.sam, c, "planner", False)          # opt out of one app
        self.assertEqual(sam.get(self.page_of("planner", things["planner"])).status_code, 404)
        self.assertNotIn("Trash night", Client().get(API + f"schedule?start={self.today}&end={self.today}", **auth).content.decode())
        self.assertEqual(sam.get(self.page_of("notes", things["notes"])).status_code, 200)
        circles.leave(self.sam, c)                                         # leave the circle
        for app, obj in things.items():
            self.assertEqual(sam.get(self.page_of(app, obj)).status_code, 404, app)
        response = Client().generic("PATCH", API + f"notes/{things['notes'].pk}", json.dumps({"append": "x"}),
                                    content_type="application/json", **auth)
        self.assertEqual(response.status_code, 404)
        self.assertFalse(Client().get(API + "meals/recipes?scope=shared", **auth).json()["recipes"])

    def test_only_owners_reshare_or_delete(self):
        things = self.things()
        sam = self.as_user(self.sam)
        for app, obj in things.items():
            with self.subTest(app=app):
                share_with(obj, self.sam, "edit")
                response = sam.post(reverse("labs:share", args=[sharing.kind_of(obj).label, obj.pk]),
                                    {"action": "add", "target": "person", "who": "kim"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
                self.assertEqual(response.status_code, 400)
                self.assertEqual(grants_of(obj), [(self.sam.pk, "edit")])
                self.assertNotIn('class="share-add"', sam.get(self.page_of(app, obj)).content.decode())
        self.assertEqual(sam.post(reverse("albright_planner:remove", args=[things["planner"].pk]), {"scope": "all"}).status_code, 404)
        self.assertEqual(sam.post(reverse("albright_notes:delete", args=[things["notes"].pk])).status_code, 404)
        self.assertEqual(sam.post(reverse("albright_fitness:delete_workout", args=[things["fitness"].pk])).status_code, 404)
        self.assertTrue(Workout.objects.filter(pk=things["fitness"].pk).exists())

    def test_deleting_a_thing_deletes_its_shares(self):
        obj = self.things()["notes"]
        share_with(obj, self.sam)
        obj.delete()
        self.assertFalse(ShareGrant.objects.exists())


# ---------------------------------------------------------------------------
# The Share control and Fitness's private data
# ---------------------------------------------------------------------------

class ControlTests(Base):
    def test_on_every_view_edit_and_new_screen(self):
        things = self.things()
        pages = [self.page_of(app, obj) for app, obj in things.items()]
        pages += [reverse("albright_planner:edit", args=[things["planner"].pk]),
                  reverse("albright_meals:recipe_edit", args=[things["meals"].pk])]
        for path in pages:
            self.assertIn('id="share-box"', self.client.get(path).content.decode(), path)
        for path in (reverse("albright_planner:new"), reverse("albright_meals:recipe_add")):
            self.assertIn('name="share_mode"', self.client.get(path).content.decode(), path)

    def test_circle_choices_and_explanation(self):
        c = self.family()
        other = circles.create(self.nate, "Book club", ["notes"])        # doesn't share the Planner
        item = self.things()["planner"]
        page = self.client.get(self.page_of("planner", item)).content.decode()
        box = page.split('id="share-box"', 1)[1].split("</section>", 1)[0]
        self.assertIn(f'value="circle:{c.pk}"', box)
        self.assertNotIn(f'value="circle:{other.pk}"', box)
        self.assertIn("Only me", box)
        self.client.post(reverse("labs:share", args=["albright_planner.ScheduleItem", item.pk]),
                         {"action": "add", "target": f"circle:{c.pk}", "permission": "edit"})
        box = self.client.get(self.page_of("planner", item)).content.decode().split('id="share-box"', 1)[1]
        self.assertIn("Albright Family members who use Planner can see and check off and change this item.", box)
        response = self.client.post(reverse("labs:share", args=["albright_planner.ScheduleItem", item.pk]),
                                    {"action": "add", "target": f"circle:{other.pk}"}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 400)

    def test_fitness_diary_and_body_are_never_shareable(self):
        from albright_fitness.models import FoodLogEntry, Measurement, NutritionProfile, WeightEntry
        for model in (FoodLogEntry, WeightEntry, Measurement, NutritionProfile):
            self.assertIsNone(sharing.kind_of(model))
        self.assertEqual(self.client.post(reverse("labs:share", args=["albright_fitness.WeightEntry", 1]), {"action": "add"}).status_code, 404)
        for name in ("food", "body", "targets"):
            self.assertNotIn('id="share-box"', self.client.get(reverse(f"albright_fitness:{name}")).content.decode(), name)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

class DefaultTests(Base):
    def test_defaults_on_create_quick_add_and_bots(self):
        c = self.family()
        for app in APPS:
            sharing.save_default(self.nate, app, "circle", c.pk, "edit")
        self.client.post(reverse("albright_planner:quick_add"), {"text": "Call plumber tomorrow"})
        self.client.post(reverse("albright_planner:new"), {"title": "Date night", "kind": "event", "date": self.today.isoformat(),
                                                           "repeat": "none", "ends": "never", "priority": "none"})
        self.client.post(reverse("albright_meals:recipe_add"), {"title": "Tacos", "servings": "4"})
        allow_api_keys(self.nate)
        _t, raw = BotToken.issue(self.nate, "bot", ["schedule:write", "notes:write", "meals:write", "fitness:write"])
        auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        post = lambda path, body: Client().post(API + path, json.dumps(body), content_type="application/json", **auth)  # noqa: E731
        self.assertEqual(post("schedule", {"title": "Bot task", "date": self.today.isoformat()}).status_code, 201)
        self.assertEqual(post("notes", {"title": "Bot note"}).status_code, 201)
        self.assertEqual(post("meals/recipes", {"title": "Bot stew"}).status_code, 201)
        self.assertEqual(post("fitness/workouts", {"text": "pushups 20x3"}).status_code, 201)
        made = [ScheduleItem.objects.get(title__icontains="plumber"), ScheduleItem.objects.get(title="Date night"),
                Recipe.objects.get(title="Tacos"), ScheduleItem.objects.get(title="Bot task"), Note.objects.get(title="Bot note"),
                Recipe.objects.get(title="Bot stew"), Workout.objects.get(owner=self.nate, name="Quick log")]
        for obj in made:
            self.assertTrue(sharing.can_edit(self.sam, obj), obj)

    def test_bot_share_field_validated_against_the_owners_circles(self):
        c = self.family()
        theirs = circles.create(self.kim, "Kim's club", APPS)
        allow_api_keys(self.nate)
        _t, raw = BotToken.issue(self.nate, "bot", ["notes:write"])
        auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        post = lambda body: Client().post(API + "notes", json.dumps(body), content_type="application/json", **auth)  # noqa: E731
        bad = post({"title": "Sneaky", "share": {"circle": theirs.pk}})
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(Note.objects.filter(title="Sneaky").exists())                 # nothing was made
        good = post({"title": "For the family", "share": {"circle": c.pk, "permission": "edit"}})
        self.assertEqual((good.status_code, good.json()["shared_with"]), (201, [{"circle": c.pk, "user": None, "permission": "edit"}]))
        person = post({"title": "For Sam", "share": {"user": "sam"}})
        self.assertEqual(person.json()["shared_with"], [{"circle": None, "user": "sam", "permission": "view"}])

    def test_the_defaults_page(self):
        c = self.family()
        page = self.client.get(reverse("labs:sharing_defaults")).content.decode()
        for app in APPS:
            self.assertIn(f'id="default-{app}"', page)
        self.client.post(reverse("labs:sharing_defaults"), {"app": "notes", "mode": f"circle:{c.pk}", "permission": "edit"})
        self.assertEqual(sharing.default_text(self.nate, "notes"), "Albright Family (can edit)")
        self.client.post(reverse("labs:sharing_defaults"), {"app": "planner", "mode": "people", "who": "kim", "permission": "view"})
        self.assertEqual(SharingDefault.objects.get(user=self.nate, app="planner").people, {str(self.kim.pk): "view"})
        self.client.post(reverse("labs:sharing_defaults"), {"app": "planner", "mode": "people", "who": "k"})   # not exact
        self.assertEqual(SharingDefault.objects.get(user=self.nate, app="planner").people, {str(self.kim.pk): "view"})


# ---------------------------------------------------------------------------
# Shared with me
# ---------------------------------------------------------------------------

class HubTests(Base):
    def test_lists_everything_shared_with_me_with_filters(self):
        c = self.family()
        things = self.things()
        share_with(things["planner"], circle=c, permission="edit")
        share_with(things["notes"], self.sam, "view")
        share_with(things["meals"], self.sam, "edit")
        Note.objects.create(owner=self.sam, title="Sam's own note")
        sam = self.as_user(self.sam)
        page = sam.get(reverse("labs:shared_with_me")).content.decode()
        for title in ("Trash night", "Packing list", "Nate&#x27;s chili"):
            self.assertIn(title, page)
        self.assertNotIn("Leg day", page)
        self.assertNotIn("Sam&#x27;s own note", page)
        self.assertIn("Albright Family", page)
        only_notes = sam.get(reverse("labs:shared_with_me") + "?app=notes").content.decode()
        self.assertIn("Packing list", only_notes)
        self.assertNotIn("Trash night", only_notes)
        by_circle = sam.get(reverse("labs:shared_with_me") + f"?circle={c.pk}").content.decode()
        self.assertIn("Trash night", by_circle)
        self.assertNotIn("Packing list", by_circle)

    def test_notes_shared_with_me_grouped_by_circle_and_person(self):
        c = self.family()
        a = Note.objects.create(owner=self.nate, title="Circle note")
        b = Note.objects.create(owner=self.kim, title="Kim's note")
        share_with(a, circle=c)
        share_with(b, self.sam)
        page = self.as_user(self.sam).get(reverse("albright_notes:list") + "?view=shared").content.decode()
        self.assertIn('<h3 class="nt-group">Albright Family</h3>', page)
        self.assertIn('<h3 class="nt-group">From Kim</h3>', page)


# ---------------------------------------------------------------------------
# The migration (labs 0010; Planner 0012-0013, Notes 0002-0003, Meals 0012-0013, Fitness 0005-0006)
# ---------------------------------------------------------------------------

class ShareMigrationTests(TransactionTestCase):
    serialized_rollback = True
    BEFORE = [("albright_labs", "0009_remove_household"), ("albright_planner", "0011_view_options"),
              ("albright_notes", "0001_initial"), ("albright_meals", "0011_spaces_required"),
              ("albright_fitness", "0004_body_metrics")]

    def _post_teardown(self):
        super()._post_teardown()
        saved = getattr(connection, "_test_serialized_contents", None)
        if saved:
            connection.creation.deserialize_db_from_string(saved)

    def test_everyone_sees_and_edits_exactly_what_they_did(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        today = date.today()
        try:
            executor.migrate(self.BEFORE)
            old = MigrationExecutor(connection).loader.project_state(self.BEFORE).apps
            nate = User.objects.create_user("nate", password=PASSWORD)
            sam = User.objects.create_user("sam", password=PASSWORD)
            kim = User.objects.create_user("kim", password=PASSWORD)
            OldSpace = old.get_model("albright_labs", "Space")
            space = OldSpace.objects.create(user_id=nate.pk)
            item = old.get_model("albright_planner", "ScheduleItem").objects.create(owner_id=nate.pk, title="Trash night", date=today)
            old.get_model("albright_planner", "ItemShare").objects.create(item=item, user_id=sam.pk, permission="edit")
            old.get_model("albright_planner", "ItemShare").objects.create(item=item, user_id=kim.pk, permission="view")
            note = old.get_model("albright_notes", "Note").objects.create(owner_id=nate.pk, title="Packing list")
            old.get_model("albright_notes", "NoteShare").objects.create(note=note, user_id=sam.pk, permission="view")
            recipe = old.get_model("albright_meals", "Recipe").objects.create(space=space, title="Chili", created_by_id=nate.pk)
            old.get_model("albright_meals", "RecipeShare").objects.create(recipe=recipe, shared_with_id=kim.pk, shared_by_id=nate.pk)
            workout = old.get_model("albright_fitness", "Workout").objects.create(owner_id=nate.pk, date=today)
            old.get_model("albright_fitness", "WorkoutShare").objects.create(workout=workout, user_id=sam.pk)
            old.get_model("albright_planner", "PlannerSettings").objects.create(user_id=nate.pk, default_shares={str(sam.pk): "edit"})
        finally:
            MigrationExecutor(connection).migrate(latest)
        for user in (nate, sam, kim):
            with_apps(user, APPS)
        item, note = ScheduleItem.objects.get(title="Trash night"), Note.objects.get(title="Packing list")
        recipe, workout = Recipe.objects.get(title="Chili"), Workout.objects.get(owner=nate)
        expected = {  # (can_view, can_edit) per person per thing - exactly the old shares
            (item, sam): (True, True), (item, kim): (True, False), (note, sam): (True, False), (note, kim): (False, False),
            (recipe, kim): (True, False), (recipe, sam): (False, False), (workout, sam): (True, False), (workout, kim): (False, False)}
        for (obj, user), (view, edit) in expected.items():
            self.assertEqual((sharing.can_view(user, obj), sharing.can_edit(user, obj)), (view, edit), f"{obj} / {user}")
        self.assertEqual(ShareGrant.objects.count(), 5)
        self.assertEqual(sharing.default_text(nate, "planner"), "sam")
        self.assertEqual(ShareGrant.objects.get(object_id=recipe.pk, content_type__model="recipe").created_by, nate)
