"""Circles (albright_labs.circles - the one place membership is checked): invites (hashed token, 14-day expiry, accept
with app choices, decline, revoke, email invites through sign-up), the per-app opt-in before anything shows, leaving
or opting out taking effect at once (pages and the Bot API), owners never seeing members' personal data, the
last-owner rule, Personal spaces made automatically, switching spaces, the Circles pages and badge - and the
household migration keeping exactly what each person could see in Meals and Money."""
import hashlib
import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotToken
from albright_meals.models import MealIdea, MealPlanEntry, Recipe
from albright_money.models import Account

from . import circles
from .models import Circle, CircleInvite, CircleMembership, Invitation, Space, UserPreference
from .testing import allow_api_keys, kitchen, personal, with_apps

PASSWORD = "pw-not-real-12345"
API = "/api/bot/v1/"


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.nate = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nathan",
                                                       email="nate@example.com"), ["meals", "money", "planner"])
        self.sam = with_apps(User.objects.create_user(username="sam", password=PASSWORD, first_name="Sam",
                                                      email="sam@example.com"), ["meals", "money"])
        self.kim = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim"), ["meals", "money"])
        self.client.force_login(self.nate)
        self.today = timezone.localdate()

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c

    def circle(self, name="Albright Family", apps=("meals", "money")):
        return circles.create(self.nate, name, apps)


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------

class InviteTests(Base):
    def test_hashed_token_and_14_days(self):
        circle = self.circle()
        invite, raw, site, _site_raw = circles.invite(self.nate, circle, "SAM")       # exact username, any case
        self.assertIsNone(site)
        self.assertEqual(invite.user, self.sam)
        self.assertEqual(invite.token_hash, hashlib.sha256(raw.encode()).hexdigest())
        self.assertNotIn(raw, json.dumps(list(CircleInvite.objects.values_list("token_hash", "email"))))
        self.assertAlmostEqual((invite.expires_at - timezone.now()).days, 13, delta=1)
        self.assertEqual(circles.pending_invites(self.sam), [invite])
        CircleInvite.objects.filter(pk=invite.pk).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(circles.pending_invites(self.sam), [])
        self.assertEqual(self.as_user(self.sam).get(reverse("labs:circle_invite", args=[invite.pk])).status_code, 404)

    def test_link_only_works_for_the_invited_person(self):
        circle = self.circle()
        invite, raw, _s, _r = circles.invite(self.nate, circle, "sam@example.com")
        response = self.as_user(self.sam).get(reverse("labs:circle_join", args=[raw]))
        self.assertRedirects(response, reverse("labs:circle_invite", args=[invite.pk]), fetch_redirect_response=False)
        self.assertEqual(self.as_user(self.kim).get(reverse("labs:circle_join", args=[raw])).status_code, 404)
        self.assertEqual(self.as_user(self.sam).get(reverse("labs:circle_join", args=["nope"])).status_code, 404)

    def test_accept_joins_only_the_apps_chosen(self):
        circle = self.circle()
        invite, _raw, _s, _r = circles.invite(self.nate, circle, "sam")
        sam = self.as_user(self.sam)
        page = sam.get(reverse("labs:circle_invite", args=[invite.pk])).content.decode()
        self.assertIn("Nathan</strong> invited you", page)
        self.assertNotIn("checked", page.split('id="circle-accept"', 1)[1].split("</form>", 1)[0])   # all off
        sam.post(reverse("labs:circle_invite", args=[invite.pk]), {"action": "accept", "apps": ["money", "notes"]})
        m = CircleMembership.objects.get(circle=circle, user=self.sam)
        self.assertEqual((m.status, m.role, m.apps), ("active", "member", ["money"]))   # notes isn't enabled
        self.assertIsNotNone(CircleInvite.objects.get(pk=invite.pk).accepted_at)

    def test_decline_and_revoke(self):
        circle = self.circle()
        invite, _raw, _s, _r = circles.invite(self.nate, circle, "sam")
        self.as_user(self.sam).post(reverse("labs:circle_invite", args=[invite.pk]), {"action": "decline"})
        self.assertEqual(CircleInvite.objects.get(pk=invite.pk).status, "declined")
        self.assertFalse(CircleMembership.objects.filter(circle=circle, user=self.sam).exists())
        again, _raw, _s, _r = circles.invite(self.nate, circle, "sam")
        self.client.post(reverse("labs:circle", args=[circle.pk]), {"action": "revoke", "invite": again.pk})
        self.assertEqual(CircleInvite.objects.get(pk=again.pk).status, "revoked")
        self.assertEqual(self.as_user(self.sam).post(reverse("labs:circle_invite", args=[again.pk]), {"action": "accept"}).status_code, 404)
        self.assertFalse(CircleMembership.objects.filter(circle=circle, user=self.sam, status="active").exists())

    def test_exact_names_only_and_email_invites_through_sign_up(self):
        circle = self.circle()
        for partial in ("sa", "Sam Smith", "@example.com"):
            with self.assertRaises(circles.CircleError):
                circles.invite(self.nate, circle, partial)
        ci, _raw, site, site_raw = circles.invite(self.nate, circle, "new.person@example.com")
        self.assertEqual((site.email, site.circle_invite), ("new.person@example.com", ci))
        self.assertEqual(sorted(site.apps), ["meals", "money"])                 # the circle's apps the inviter has
        response = Client().post(reverse("labs:signup", args=[site_raw]), {
            "username": "robin", "password1": "a-Strong-pass-123!", "password2": "a-Strong-pass-123!"})
        self.assertRedirects(response, reverse("labs:circles"), fetch_redirect_response=False)
        robin = User.objects.get(username="robin")
        self.assertEqual(circles.pending_invites(robin), [CircleInvite.objects.get(pk=ci.pk)])
        self.assertFalse(CircleMembership.objects.filter(circle=circle, user=robin, status="active").exists())

    def test_only_owners_invite(self):
        circle = self.circle()
        invite, _raw, _s, _r = circles.invite(self.nate, circle, "sam")
        circles.accept(self.sam, invite, ["meals"])
        with self.assertRaises(circles.CircleError):
            circles.invite(self.sam, circle, "kim")


# ---------------------------------------------------------------------------
# Who sees what
# ---------------------------------------------------------------------------

class VisibilityTests(Base):
    def setUp(self):
        super().setUp()
        self.c = self.circle()
        self.space = Space.objects.get(circle=self.c)
        invite, _raw, _s, _r = circles.invite(self.nate, self.c, "sam")
        self.invite = invite
        self.recipe = Recipe.objects.create(space=self.space, title="Family Meatballs", created_by=self.nate)
        self.joint = Account.objects.create(owner=self.nate, space=self.space, name="Joint", starting_date=self.today)

    def test_nothing_shows_until_accepted_and_opted_in(self):
        sam = self.as_user(self.sam)
        self.assertEqual(sam.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).status_code, 404)  # invited only
        circles.accept(self.sam, self.invite, [])                                   # joined, nothing opted in
        self.assertEqual(sam.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).status_code, 404)
        self.assertEqual(sam.get(reverse("albright_money:account", args=[self.joint.pk])).status_code, 404)
        circles.set_participation(self.sam, self.c, "meals", True)
        self.assertEqual(sam.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).status_code, 200)
        self.assertEqual(sam.get(reverse("albright_money:account", args=[self.joint.pk])).status_code, 404)  # money still off
        circles.set_participation(self.sam, self.c, "money", True)
        self.assertEqual(sam.get(reverse("albright_money:account", args=[self.joint.pk])).status_code, 200)

    def test_needs_the_app_on_the_site_and_enabled_in_the_circle(self):
        circles.accept(self.sam, self.invite, ["meals", "money"])
        with_apps(self.sam, ["money"])                                               # Meals taken away on the site
        self.assertNotIn(self.space, circles.visible_spaces(self.sam, "meals"))
        with_apps(self.sam, ["meals", "money"])
        circles.set_enabled_apps(self.nate, self.c, ["money"])                       # the owner turns Meals off
        self.assertNotIn(self.space, circles.visible_spaces(self.sam, "meals"))
        self.assertNotIn(self.space, circles.visible_spaces(self.nate, "meals"))
        self.assertIn(self.space, circles.visible_spaces(self.sam, "money"))

    def test_leaving_or_opting_out_is_immediate_pages_and_bot_api(self):
        circles.accept(self.sam, self.invite, ["meals", "money"])
        allow_api_keys(self.sam)
        _t, raw = BotToken.issue(self.sam, "bot", ["meals:read"])
        auth = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        sam = self.as_user(self.sam)
        self.assertEqual(Client().get(API + f"meals/recipes?space={self.space.pk}", **auth).status_code, 200)
        circles.set_participation(self.sam, self.c, "meals", False)
        self.assertEqual(sam.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).status_code, 404)
        self.assertEqual(Client().get(API + f"meals/recipes?space={self.space.pk}", **auth).status_code, 404)
        self.assertEqual(sam.get(reverse("albright_money:account", args=[self.joint.pk])).status_code, 200)   # money still on
        circles.leave(self.sam, self.c)
        self.assertEqual(sam.get(reverse("albright_money:account", args=[self.joint.pk])).status_code, 404)
        self.assertTrue(Account.objects.filter(pk=self.joint.pk).exists())          # the circle's data stays

    def test_removed_member_loses_access_and_keeps_their_own(self):
        circles.accept(self.sam, self.invite, ["meals", "money"])
        hers = Recipe.objects.create(space=personal(self.sam), title="Sam's secret soup", created_by=self.sam)
        circles.remove(self.nate, self.c, self.sam)
        sam = self.as_user(self.sam)
        self.assertEqual(sam.get(reverse("albright_meals:recipe", args=[self.recipe.pk])).status_code, 404)
        self.assertEqual(sam.get(reverse("albright_meals:recipe", args=[hers.pk])).status_code, 200)

    def test_owners_never_see_members_personal_data(self):
        circles.accept(self.sam, self.invite, ["meals", "money"])
        soup = Recipe.objects.create(space=personal(self.sam), title="Sam's secret soup", created_by=self.sam)
        card = Account.objects.create(owner=self.sam, space=personal(self.sam), name="Sam's card", starting_date=self.today)
        MealIdea.objects.create(space=personal(self.sam), title="Sam's idea")
        self.assertEqual(self.client.get(reverse("albright_meals:recipe", args=[soup.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("albright_money:account", args=[card.pk])).status_code, 404)
        for path in (reverse("albright_meals:recipes") + f"?space={self.space.pk}", reverse("albright_meals:ideas"),
                     reverse("albright_money:dashboard") + "?all=1", reverse("labs:circle", args=[self.c.pk])):
            page = self.client.get(path).content.decode()
            for secret in ("Sam's secret soup", "Sam's card", "Sam's idea"):
                self.assertNotIn(secret, page, path)

    def test_another_circles_data_is_404_everywhere(self):
        theirs = kitchen(self.kim, name="Kim's circle")
        their_recipe = Recipe.objects.create(space=theirs, title="Kim's chili", created_by=self.kim)
        entry = MealPlanEntry.objects.create(space=theirs, date=self.today, slot="dinner", title="Kim's dinner")
        account = Account.objects.create(owner=self.kim, space=theirs, name="Kim's joint", starting_date=self.today)
        for path in (reverse("albright_meals:recipe", args=[their_recipe.pk]), reverse("albright_meals:plan_entry", args=[entry.pk]),
                     reverse("albright_money:account", args=[account.pk]), reverse("labs:circle", args=[theirs.circle.pk])):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        self.assertEqual(self.client.get(reverse("albright_meals:week") + f"?space={theirs.pk}").status_code, 200)
        self.assertNotIn("Kim's dinner", self.client.get(reverse("albright_meals:week") + f"?space={theirs.pk}").content.decode())


class OwnershipTests(Base):
    def test_last_owner_must_hand_over_first(self):
        c = self.circle()
        invite, _raw, _s, _r = circles.invite(self.nate, c, "sam")
        circles.accept(self.sam, invite, ["meals"])
        with self.assertRaises(circles.CircleError):
            circles.leave(self.nate, c)
        page = self.client.post(reverse("labs:circle", args=[c.pk]), {"action": "leave"}, follow=True).content.decode()
        self.assertIn("only owner", page)
        self.client.post(reverse("labs:circle", args=[c.pk]), {"action": "make_owner", "user": self.sam.pk})
        self.assertEqual(CircleMembership.objects.get(circle=c, user=self.sam).role, "owner")
        self.assertEqual(CircleMembership.objects.get(circle=c, user=self.nate).role, "member")
        circles.leave(self.nate, c)
        self.assertEqual(CircleMembership.objects.get(circle=c, user=self.nate).status, "left")

    def test_owner_settings_are_owner_only(self):
        c = self.circle()
        invite, _raw, _s, _r = circles.invite(self.nate, c, "sam")
        circles.accept(self.sam, invite, ["meals"])
        sam = self.as_user(self.sam)
        sam.post(reverse("labs:circle", args=[c.pk]), {"action": "rename", "name": "Sam's now"})
        sam.post(reverse("labs:circle", args=[c.pk]), {"action": "remove", "user": self.nate.pk})
        c.refresh_from_db()
        self.assertEqual(c.name, "Albright Family")
        self.assertEqual(CircleMembership.objects.get(circle=c, user=self.nate).status, "active")
        self.client.post(reverse("labs:circle", args=[c.pk]), {"action": "rename", "name": "The Albrights"})
        c.refresh_from_db()
        self.assertEqual(c.name, "The Albrights")


class SpacesTests(Base):
    def test_personal_spaces_are_made_automatically(self):
        self.assertFalse(Space.objects.filter(user=self.kim).exists())
        self.assertEqual(self.as_user(self.kim).get(reverse("albright_meals:week")).status_code, 200)
        self.assertTrue(Space.objects.filter(user=self.kim).exists())
        self.assertEqual([s.name for s in circles.visible_spaces(self.kim, "money")], ["Personal"])

    def test_switching_spaces_is_remembered(self):
        c = self.circle()
        shared = Space.objects.get(circle=c)
        MealPlanEntry.objects.create(space=shared, date=self.today, slot="dinner", title="Circle tacos")
        MealPlanEntry.objects.create(space=personal(self.nate), date=self.today, slot="dinner", title="Solo soup")
        page = self.client.get(reverse("albright_meals:week")).content.decode()
        self.assertIn('id="space-switch"', page)
        self.assertIn("Solo soup", page)                                           # Personal first
        self.assertNotIn("Circle tacos", page)
        page = self.client.get(reverse("albright_meals:week") + f"?space={shared.pk}").content.decode()
        self.assertIn("Circle tacos", page)
        self.assertIn("Circle tacos", self.client.get(reverse("albright_meals:week")).content.decode())   # remembered
        self.assertEqual(UserPreference.objects.get(user=self.nate).current_spaces["meals"], shared.pk)

    def test_money_overview_of_all_my_spaces(self):
        c = self.circle()
        Account.objects.create(owner=self.nate, space=Space.objects.get(circle=c), name="Joint", starting_date=self.today)
        Account.objects.create(owner=self.nate, space=personal(self.nate), name="My card", starting_date=self.today)
        page = self.client.get(reverse("albright_money:dashboard") + "?all=1").content.decode()
        self.assertIn('id="mn-overview"', page)
        self.assertIn("Joint", page)
        self.assertIn("My card", page)
        self.assertIn("All my spaces", page)


class PagesTests(Base):
    def test_create_toggle_and_badge(self):
        self.client.post(reverse("labs:circles"), {"action": "create", "name": "Book club", "apps": ["meals", "money"], "invite": "sam"})
        c = Circle.objects.get(name="Book club")
        self.assertEqual(c.enabled_apps, ["meals", "money"])
        self.assertEqual(CircleMembership.objects.get(circle=c, user=self.nate).role, "owner")
        sam = self.as_user(self.sam)
        home = sam.get(reverse("labs:account")).content.decode()
        self.assertIn('id="circle-invite-badge"', home)
        circles.accept(self.sam, CircleInvite.objects.get(circle=c), [])
        self.assertNotIn('id="circle-invite-badge"', sam.get(reverse("labs:account")).content.decode())
        sam.post(reverse("labs:circles"), {"action": "participate", "circle": c.pk, "app": "meals", "on": "1"})
        self.assertEqual(CircleMembership.objects.get(circle=c, user=self.sam).apps, ["meals"])
        page = sam.get(reverse("labs:circles")).content.decode()
        self.assertIn('aria-checked="true" aria-label="Meals in Book club"', page)
        self.assertEqual(self.as_user(self.kim).get(reverse("labs:circle", args=[c.pk])).status_code, 404)

    def test_old_household_page_goes_to_circles(self):
        self.assertRedirects(self.client.get("/home/account/household/"), reverse("labs:circles"), fetch_redirect_response=False)
        self.assertIn(reverse("labs:circles"), self.client.get(reverse("labs:account")).content.decode())


# ---------------------------------------------------------------------------
# The household migration (albright_labs 0008, Meals 0009-0011, Money 0003-0005, labs 0009)
# ---------------------------------------------------------------------------

class HouseholdMigrationTests(TransactionTestCase):
    """Runs the real migrations: back to households, make some data, forward again - then each person sees exactly
    what they saw before."""
    serialized_rollback = True                       # put the seeded rows (e.g. the exercise library) back afterwards

    def _post_teardown(self):
        """Django empties every table after a TransactionTestCase and only puts the saved rows back before the NEXT
        one - with a kept test database, that would leave later test runs without the seeded data. Put it back now."""
        super()._post_teardown()
        saved = getattr(connection, "_test_serialized_contents", None)
        if saved:
            connection.creation.deserialize_db_from_string(saved)

    BEFORE = [("albright_labs", "0007_household"), ("albright_meals", "0008_nutrition"),
              ("albright_money", "0002_household_moved_to_labs")]

    def test_everyone_keeps_what_they_saw(self):
        executor = MigrationExecutor(connection)
        latest = executor.loader.graph.leaf_nodes()
        try:
            executor.migrate(self.BEFORE)
            old = MigrationExecutor(connection).loader.project_state(self.BEFORE).apps
            nate = User.objects.create_user("nate", password=PASSWORD, first_name="Nathan")
            sam = User.objects.create_user("sam", password=PASSWORD, first_name="Sam")
            kim = User.objects.create_user("kim", password=PASSWORD, first_name="Kim")
            Household = old.get_model("albright_labs", "Household")
            Member = old.get_model("albright_labs", "HouseholdMember")
            OldAccount = old.get_model("albright_money", "Account")
            OldRecipe = old.get_model("albright_meals", "Recipe")
            OldEntry = old.get_model("albright_meals", "MealPlanEntry")
            OldInvitation = old.get_model("albright_labs", "Invitation")
            h = Household.objects.create(name="The Albrights", created_by_id=nate.pk)
            Member.objects.create(household=h, user_id=nate.pk)
            Member.objects.create(household=h, user_id=sam.pk)
            today = timezone.localdate()
            accounts = {name: OldAccount.objects.create(owner_id=owner.pk, household=household, name=name, starting_date=today).pk
                        for name, owner, household in (("Joint", nate, h), ("Nate's card", nate, None),
                                                       ("Sam's savings", sam, None), ("Kim's checking", kim, None))}
            recipe = OldRecipe.objects.create(household=h, title="Family Meatballs", created_by_id=nate.pk).pk
            OldEntry.objects.create(household=h, date=today, slot="dinner", title="Tacos")
            OldInvitation.objects.create(email="new@example.com", token_hash="x" * 64, created_by_id=nate.pk,
                                         expires_at=timezone.now() + timedelta(days=3), household=h)
        finally:
            MigrationExecutor(connection).migrate(latest)
        for user in (nate, sam, kim):
            with_apps(user, ["meals", "money"])
        circle = Circle.objects.get(name="The Albrights")
        self.assertEqual(circle.enabled_apps, ["meals", "money"])
        members = {m.user.username: (m.role, m.status, m.apps) for m in circle.memberships.select_related("user")}
        self.assertEqual(members, {"nate": ("owner", "active", ["meals", "money"]),
                                   "sam": ("member", "active", ["meals", "money"])})
        sees = lambda user: sorted(Account.objects.visible_to(user).values_list("name", flat=True))  # noqa: E731
        self.assertEqual(sees(nate), ["Joint", "Nate's card"])
        self.assertEqual(sees(sam), ["Joint", "Sam's savings"])
        self.assertEqual(sees(kim), ["Kim's checking"])
        self.assertEqual(Account.objects.get(pk=accounts["Nate's card"]).space, Space.objects.get(user=nate))
        from albright_meals import scope
        for user, expected in ((nate, True), (sam, True), (kim, False)):
            self.assertEqual(scope.readable(user).filter(pk=recipe).exists(), expected, user.username)
        self.assertEqual(MealPlanEntry.objects.get(title="Tacos").space, circle.space)
        from . import circles as circle_rules
        for user in (nate, sam):                                          # they open on the circle, as before
            self.assertEqual(circle_rules.current_space(user, "meals"), circle.space)
            self.assertEqual(circle_rules.current_space(user, "money"), circle.space)
        self.assertEqual(circle_rules.current_space(kim, "money"), Space.objects.get(user=kim))
        invite = Invitation.objects.get(email="new@example.com")
        self.assertEqual((invite.circle_invite.circle, invite.circle_invite.status), (circle, "pending"))
