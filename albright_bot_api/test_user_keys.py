"""API keys for people other than the site owner: a key acts as its owner (their items, what they can see), only
with scopes for apps they can open (admin-only scopes are for superusers - in the form and on every request),
"Allow API keys" (Admin > Users) gates the page and the keys, the site owner can see and revoke anyone's keys,
keys are stored hashed and shown once, every change is in the audit log, each person sees their own activity,
and the docs list only what the calling key can use."""
from datetime import date

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from albright_control.models import AuditEntry
from albright_labs.models import UserPreference
from albright_labs.testing import allow_api_keys, kitchen, with_apps
from albright_planner.models import ItemShare, ScheduleItem

from .models import BotRequestLog, BotToken, hash_token

PASSWORD = "pw-not-real-12345"
API = "/api/bot/v1/"
TODAY = date.today()


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class Base(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(username="nate", password=PASSWORD, email="", first_name="Nathan")
        self.wife = allow_api_keys(with_apps(User.objects.create_user(username="sam", password=PASSWORD,
                                                                      first_name="Sam"),
                                             ["meals", "notes", "planner"]))
        kitchen(self.boss, self.wife)
        self.client.force_login(self.wife)
        self.api = Client()

    def call(self, method, path, raw, body=None):
        import json
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        if method == "GET":
            return self.api.get(API + path, **headers)
        return getattr(self.api, method.lower())(API + path, data=json.dumps(body or {}),
                                                 content_type="application/json", **headers)

    def create_key(self, scopes, name="Sam's bot", client=None, password=PASSWORD):
        response = (client or self.client).post(reverse("albright_bot_api:my_keys"),
                                                {"name": name, "scopes": scopes, "password": password})
        raw = None
        if response.status_code == 200 and response.context and response.context.get("new_key"):
            raw = response.context["new_key"]
        return response, raw


class ActsAsOwnerTests(Base):
    def test_her_key_creates_planner_items_owned_by_her(self):
        _response, raw = self.create_key(["schedule:read", "schedule:write"])
        response = self.call("POST", "schedule", raw, {"title": "Pilates", "date": TODAY.isoformat()})
        self.assertEqual(response.status_code, 201)
        item = ScheduleItem.objects.get(title="Pilates")
        self.assertEqual((item.owner, item.created_by, item.from_bot), (self.wife, "Sam's bot", True))

    def test_she_never_sees_my_private_items_but_does_see_shared_ones(self):
        _response, raw = self.create_key(["schedule:read"])
        ScheduleItem.objects.create(owner=self.boss, title="Surprise party plans", date=TODAY)
        shared = ScheduleItem.objects.create(owner=self.boss, title="Family dinner", date=TODAY)
        ItemShare.objects.create(item=shared, user=self.wife)
        titles = [o["title"] for o in self.call("GET", f"schedule?start={TODAY}&end={TODAY}", raw).json()["occurrences"]]
        self.assertEqual(titles, ["Family dinner"])
        row = self.call("GET", f"schedule?start={TODAY}&end={TODAY}", raw).json()["occurrences"][0]
        self.assertEqual((row["owner"], row["mine"]), ("Nathan", False))

    def test_new_items_use_her_default_share(self):
        from albright_planner.models import PlannerSettings
        PlannerSettings.objects.update_or_create(user=self.wife, defaults={"default_shares": {str(self.boss.pk): "edit"}})
        _response, raw = self.create_key(["schedule:write"])
        self.call("POST", "schedule", raw, {"title": "Vet", "date": TODAY.isoformat()})
        self.assertEqual(list(ScheduleItem.objects.get(title="Vet").shares.values_list("user", "permission")),
                         [(self.boss.pk, "edit")])

    def test_notes_and_meals_act_as_her(self):
        _response, raw = self.create_key(["notes:write", "meals:write"])
        self.assertEqual(self.call("POST", "notes", raw, {"title": "Gift ideas", "body": "x"}).status_code, 201)
        from albright_notes.models import Note
        self.assertEqual(Note.objects.get(title="Gift ideas").owner, self.wife)
        self.assertEqual(self.call("POST", "meals/groceries", raw, {"name": "milk"}).status_code, 201)
        from albright_meals.models import GroceryItem
        line = GroceryItem.objects.get(name="milk")
        self.assertEqual((line.added_by, line.from_bot), (self.wife, True))


class ScopesByAccessTests(Base):
    def test_form_offers_only_her_apps_scopes_in_plain_words(self):
        page = self.client.get(reverse("albright_bot_api:my_keys")).content.decode()
        self.assertIn("Add and check off tasks in my Planner", page)
        for scope in ("schedule:read", "meals:write", "notes:read"):
            self.assertIn(f'value="{scope}"', page)
        for scope in ("trading:notes", "reseller:read", "opportunities:read", "ideas:write", "site:suggest",
                      "suggestions:read"):
            self.assertNotIn(f'value="{scope}"', page)

    def test_admin_only_scopes_cant_be_granted_by_her(self):
        response, raw = self.create_key(["schedule:read", "trading:notes"])
        self.assertIsNone(raw)
        self.assertFalse(BotToken.objects.filter(owner=self.wife).exists())
        _response, raw = self.create_key(["schedule:read"])
        token = BotToken.objects.get(owner=self.wife)
        self.client.post(reverse("albright_bot_api:my_key_scopes", args=[token.pk]),
                         {"scopes": ["schedule:read", "ideas:read"], "password": PASSWORD})
        token.refresh_from_db()
        self.assertEqual(token.scopes, ["schedule:read"])
        # Even a key that somehow holds one is refused on every request.
        _t, sneaky = BotToken.issue(self.wife, "Sneaky", ["ideas:read", "suggestions:read"])
        response = self.call("GET", "ideas", sneaky)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["required"], "ideas:read")
        self.assertIn("admins only", response.json()["error"])
        self.assertEqual(self.call("GET", "site-suggestions/counts", sneaky).status_code, 403)
        # Admin can't hand them to her key either.
        self.client.force_login(self.boss)
        self.client.post(reverse("albright_control:edit_token_scopes", args=[token.pk]),
                         {"scopes": ["schedule:read", "trading:notes"], "password": PASSWORD})
        token.refresh_from_db()
        self.assertEqual(token.scopes, ["schedule:read"])

    def test_taking_an_app_away_stops_her_key_for_it_at_once(self):
        _response, raw = self.create_key(["schedule:read", "meals:read"])
        self.assertEqual(self.call("GET", "meals/groceries", raw).status_code, 200)
        with_apps(self.wife, ["notes", "planner"])                      # Meals removed in Admin > Users
        response = self.call("GET", "meals/groceries", raw)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"error": "Your account doesn't have access to Meals, so meals:read "
                                                    "doesn't work.", "required": "meals:read"})
        self.assertEqual(self.call("GET", f"schedule?start={TODAY}", raw).status_code, 200)   # the rest still works
        page = self.client.get(reverse("albright_bot_api:my_keys")).content.decode()
        self.assertIn("doesn't work", page)


class AllowKeysTests(Base):
    def test_off_hides_the_page_and_blocks_creating(self):
        allow_api_keys(self.wife, False)
        self.assertEqual(self.client.get(reverse("albright_bot_api:my_keys")).status_code, 404)
        response, _raw = self.create_key(["schedule:read"])
        self.assertEqual(response.status_code, 404)
        self.assertFalse(BotToken.objects.exists())
        self.assertNotIn("My API keys", self.client.get(reverse("labs:account")).content.decode())
        allow_api_keys(self.wife)
        self.assertIn(reverse("albright_bot_api:my_keys"), self.client.get(reverse("labs:account")).content.decode())

    def test_off_stops_her_existing_keys(self):
        _response, raw = self.create_key(["schedule:read"])
        allow_api_keys(self.wife, False)
        response = self.call("GET", "docs", raw)
        self.assertEqual(response.status_code, 403)
        self.assertIn("turned off", response.json()["error"])

    def test_admin_users_toggle_and_key_counts(self):
        self.create_key(["schedule:read"])
        self.create_key(["notes:read"], name="Second")
        self.client.force_login(self.boss)
        page = self.client.get(reverse("albright_control:users")).content.decode()
        cell = page.split(f'data-keys-for="{self.wife.pk}"', 1)[1].split("</td>", 1)[0]
        self.assertIn("2 active keys", cell)
        self.assertIn('aria-checked="true"', cell)
        self.client.post(reverse("albright_control:users"), {"action": "api_keys", "user_id": self.wife.pk, "allowed": "0"})
        self.assertFalse(UserPreference.objects.get(user=self.wife).api_keys_allowed)
        self.assertTrue(AuditEntry.objects.filter(action="user_api_keys", details__contains="sam").exists())

    def test_i_can_see_and_revoke_her_keys(self):
        _response, raw = self.create_key(["schedule:read"])
        token = BotToken.objects.get(owner=self.wife)
        self.client.force_login(self.boss)
        page = self.client.get(reverse("albright_control:bot_api") + f"?owner={self.wife.pk}").content.decode()
        self.assertIn("Sam&#x27;s bot", page)
        self.client.post(reverse("albright_control:revoke_token", args=[token.pk]), {"password": PASSWORD})
        token.refresh_from_db()
        self.assertFalse(token.is_active)
        self.assertEqual(self.call("GET", "docs", raw).status_code, 401)


class KeyLifecycleTests(Base):
    def test_hashed_and_shown_once(self):
        response, raw = self.create_key(["schedule:read"])
        self.assertTrue(raw.startswith("abt_"))
        self.assertContains(response, raw)
        token = BotToken.objects.get(owner=self.wife)
        self.assertEqual(token.token_hash, hash_token(raw))
        self.assertNotIn(raw, self.client.get(reverse("albright_bot_api:my_keys")).content.decode())

    def test_password_needed_to_create_edit_and_rotate(self):
        response, raw = self.create_key(["schedule:read"], password="wrong")
        self.assertIsNone(raw)
        self.assertContains(response, "That isn&#x27;t your password")
        _response, raw = self.create_key(["schedule:read"])
        token = BotToken.objects.get(owner=self.wife)
        self.client.post(reverse("albright_bot_api:my_key_scopes", args=[token.pk]), {"scopes": ["notes:read"], "password": "x"})
        self.client.post(reverse("albright_bot_api:my_key_rotate", args=[token.pk]), {"password": "x"})
        token.refresh_from_db()
        self.assertEqual((token.scopes, token.token_hash), (["schedule:read"], hash_token(raw)))

    def test_rotate_revoke_and_the_audit_log(self):
        _response, raw = self.create_key(["schedule:read"])
        token = BotToken.objects.get(owner=self.wife)
        self.client.post(reverse("albright_bot_api:my_key_scopes", args=[token.pk]),
                         {"scopes": ["schedule:read", "notes:read"], "password": PASSWORD})
        response = self.client.post(reverse("albright_bot_api:my_key_rotate", args=[token.pk]), {"password": PASSWORD})
        new_raw = response.context["new_key"]
        self.assertEqual(self.call("GET", "docs", raw).status_code, 401)
        self.assertEqual(self.call("GET", "docs", new_raw).status_code, 200)
        self.client.post(reverse("albright_bot_api:my_key_revoke", args=[token.pk]))
        self.assertEqual(self.call("GET", "docs", new_raw).status_code, 401)
        actions = list(AuditEntry.objects.filter(user=self.wife).order_by("pk").values_list("action", flat=True))
        self.assertEqual(actions, ["token_created", "token_scopes_changed", "token_rotated", "token_revoked"])

    def test_she_cant_touch_my_keys(self):
        mine, _raw = BotToken.issue(self.boss, "Grok", ["trading:notes"])
        for name in ("my_key_scopes", "my_key_rotate", "my_key_revoke"):
            self.assertEqual(self.client.post(reverse(f"albright_bot_api:{name}", args=[mine.pk]),
                                              {"password": PASSWORD}).status_code, 404)
        self.assertTrue(BotToken.objects.get(pk=mine.pk).is_active)

    def test_she_sees_only_her_keys_activity(self):
        _response, raw = self.create_key(["schedule:read"])
        _mine, my_raw = BotToken.issue(self.boss, "Grok", ["trading:notes"])
        self.call("GET", f"schedule?start={TODAY}", raw)
        self.call("GET", "trading/strategies", my_raw)
        page = self.client.get(reverse("albright_bot_api:my_keys")).content.decode()
        activity = page.split('id="my-key-activity"', 1)[1]
        self.assertIn("GET /api/bot/v1/schedule", activity)
        self.assertNotIn("trading/strategies", activity)
        self.assertEqual(BotRequestLog.objects.count(), 2)                # Admin still sees both


class DocsTests(Base):
    def test_docs_list_only_what_the_key_can_use(self):
        _response, raw = self.create_key(["schedule:read", "schedule:write"])
        text = self.call("GET", "docs", raw).content.decode()
        self.assertIn("GET  schedule?start=", text)
        self.assertIn("POST schedule/<id>/complete", text)
        self.assertIn("SCHEDULE (the owner's Planner", text)
        for hidden in ("trading/notes", "reseller/lots", "opportunities/new", "POST ideas", "site-suggestions",
                       "meals/plan", "GET  notes?", "MEALS (", "NOTES ("):
            self.assertNotIn(hidden, text)
        self.assertIn("YOUR TOKEN'S SCOPES: schedule:read, schedule:write", text)
        self.assertIn("GET  docs", text)

    def test_my_full_key_sees_everything(self):
        _t, raw = BotToken.issue(self.boss, "Grok", ["trading:notes", "meals:read"])
        text = self.call("GET", "docs", raw).content.decode()
        self.assertIn("POST trading/notes", text)
        self.assertIn("GET  meals/plan", text)
        self.assertNotIn("POST meals/plan", text)                         # meals:write not on this key
        self.assertNotIn("GET  schedule?", text)
