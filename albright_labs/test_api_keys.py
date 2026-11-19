"""My account > API keys (/account/api-keys/, api_key_views): moved out of Trading (the old address 301s), gated by
"Can create API keys" (off: a message, 403 on every POST, and existing keys disabled - not deleted - until it's back
on), Full access / Read only / Custom, an optional expiry, the key shown once with the Connect your AI tabs, and the
list (access, last used, expiry; rename, edit access, revoke)."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotToken
from albright_control.models import AuditEntry
from albright_labs.models import UserPreference

from .testing import allow_api_keys, with_apps

PASSWORD = "pw-not-real-12345"
API = "/api/bot/v1/"


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class Base(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser(username="nate", password=PASSWORD, email="", first_name="Nathan")
        self.kim = allow_api_keys(with_apps(User.objects.create_user(username="kim", password=PASSWORD),
                                            ["planner", "meals", "notes", "fitness"]))
        self.client.force_login(self.kim)
        self.api = Client()

    def create(self, client=None, **data):
        body = {"name": "My ChatGPT", "access": "full", "expiry": "never", "password": PASSWORD, **data}
        response = (client or self.client).post(reverse("labs:api_keys"), body)
        raw = response.context.get("new_key") if response.status_code == 200 and response.context else None
        return response, raw

    def get(self, path, raw):
        return self.api.get(API + path, HTTP_AUTHORIZATION=f"Bearer {raw}")


class PageTests(Base):
    def test_old_trading_address_redirects_for_good(self):
        response = self.client.get("/bot-api/keys/")
        self.assertEqual((response.status_code, response["Location"]), (301, reverse("labs:api_keys")))
        self.assertEqual(self.client.post("/bot-api/keys/4/revoke/").status_code, 301)
        self.assertEqual(reverse("labs:api_keys"), "/account/api-keys/")

    def test_renders_in_the_account_layout_without_trading(self):
        response = self.client.get(reverse("labs:api_keys"))
        self.assertEqual((response.context["current_app"], response.context["app_label"]), ("account", "Account"))
        page = response.content.decode()
        self.assertIn('data-app="account"', page)
        self.assertNotIn('data-app="trading"', page)
        self.assertIn("&larr; My account", page)
        for tab in ("ChatGPT", "Claude", "Grok", "Gemini", "Other / any AI"):
            self.assertIn(f">{tab}</button>", page)
        self.assertEqual(page.count("Copy instructions for your AI"), 5)
        self.assertIn("https://testserver/api/bot/v1/mcp", page)       # every URL is this site's real one
        self.assertIn("https://testserver/api/bot/v1/docs", page)

    def test_linked_from_my_account_and_both_menus(self):
        page = self.client.get(reverse("labs:account")).content.decode()
        self.assertIn("API keys / Connect your AI", page)
        self.assertIn('id="nav-api-keys-link"', page)                  # desktop account dropdown
        self.assertIn('id="menu-api-keys-link"', page)                 # the phone menu's Account section

    def test_name_starts_empty_with_a_placeholder(self):
        page = self.client.get(reverse("labs:api_keys")).content.decode()
        field = page.split('name="name"', 1)[1].split(">", 1)[0]
        self.assertIn('placeholder="e.g. My ChatGPT"', field)
        self.assertNotIn("value=", field)
        self.assertIn('autocomplete="username" hidden', page)          # password managers fill this, not the name


class FlagTests(Base):
    def test_without_the_flag_no_form_no_list_and_post_is_403(self):
        allow_api_keys(self.kim, False)
        page = self.client.get(reverse("labs:api_keys"))
        self.assertContains(page, "Ask Nathan to enable it.")
        self.assertNotContains(page, 'id="key-form"')
        self.assertEqual(self.create()[0].status_code, 403)
        token, _raw = BotToken.issue(self.kim, "Old", ["notes:read"])
        for name in ("api_key_revoke", "api_key_rename", "api_key_scopes", "api_key_rotate"):
            self.assertEqual(self.client.post(reverse(f"labs:{name}", args=[token.pk]), {"password": PASSWORD}).status_code, 403)
        token.refresh_from_db()
        self.assertEqual((token.name, token.is_active, token.scopes), ("Old", True, ["notes:read"]))

    def test_flag_off_disables_keys_and_on_reenables_them(self):
        _response, raw = self.create()
        self.assertEqual(self.get("me", raw).status_code, 200)
        self.client.force_login(self.boss)
        self.client.post(reverse("albright_control:users"), {"action": "api_keys", "user_id": self.kim.pk, "allowed": "0"})
        response = self.get("me", raw)
        self.assertEqual((response.status_code, response.json()["error"]), (403, "API access disabled"))
        self.assertTrue(BotToken.objects.get(owner=self.kim).is_active)            # disabled, not deleted
        self.client.post(reverse("albright_control:users"), {"action": "api_keys", "user_id": self.kim.pk, "allowed": "1"})
        self.assertEqual(self.get("me", raw).status_code, 200)

    def test_superusers_always_can(self):
        self.client.force_login(self.boss)
        self.assertFalse(UserPreference.for_user(self.boss).api_keys_allowed)
        self.assertContains(self.client.get(reverse("labs:api_keys")), 'id="key-form"')

    def test_admin_users_list_shows_flag_count_and_revokes_all(self):
        self.create()
        self.create(name="Second")
        BotToken.issue(self.kim, "Expired", ["notes:read"], expires_at=timezone.now() - timedelta(days=1))
        self.client.force_login(self.boss)
        page = self.client.get(reverse("albright_control:users")).content.decode()
        self.assertIn("Can create API keys", page)
        cell = page.split(f'data-keys-for="{self.kim.pk}"', 1)[1].split("</td>", 1)[0]
        self.assertIn("2 active keys", cell)                                        # the expired one doesn't count
        self.assertIn("Revoke all keys", cell)
        self.client.post(reverse("albright_control:users"), {"action": "revoke_keys", "user_id": self.kim.pk})
        self.assertFalse(BotToken.objects.filter(owner=self.kim, revoked_at__isnull=True).exists())
        self.assertTrue(AuditEntry.objects.filter(action="user_keys_revoked", details__contains="kim").exists())

    def test_flag_is_editable_in_django_admin(self):
        from django.contrib import admin
        self.assertIn(UserPreference, admin.site._registry)
        self.assertIn("api_keys_allowed", admin.site._registry[UserPreference].list_editable)


class AccessTests(Base):
    def scopes_for(self, **data):
        self.create(**data)
        return BotToken.objects.filter(owner=self.kim).order_by("-pk").first().scopes

    def test_full_access_is_every_app_scope_and_never_admin_ones(self):
        scopes = self.scopes_for(access="full")
        self.assertIn("schedule:write", scopes)
        self.assertIn("fitness:food:write", scopes)
        self.assertIn("activity:read", scopes)
        self.assertFalse([s for s in scopes if s.startswith(("trading", "reseller", "opportunities", "ideas", "site", "suggestions"))])

    def test_read_only_is_only_read_scopes(self):
        scopes = self.scopes_for(access="read")
        self.assertTrue(scopes)
        self.assertTrue(all(s.endswith(":read") for s in scopes), scopes)
        self.assertIn("meals:read", scopes)

    def test_custom_is_exactly_what_was_ticked(self):
        self.assertEqual(self.scopes_for(access="custom", scopes=["notes:read", "meals:write"]), ["meals:write", "notes:read"])

    def test_custom_with_nothing_ticked_is_refused(self):
        response, raw = self.create(access="custom")
        self.assertIsNone(raw)
        self.assertContains(response, "Pick at least one.", status_code=400)

    def test_non_admins_never_get_admin_scopes(self):
        response, raw = self.create(access="custom", scopes=["notes:read", "trading:notes"])
        self.assertIsNone(raw)
        self.assertFalse(BotToken.objects.exists())
        page = self.client.get(reverse("labs:api_keys")).content.decode()
        self.assertNotIn('value="trading:notes"', page)

    def test_admin_full_access_leaves_admin_scopes_out_but_custom_can_add_them(self):
        self.client.force_login(self.boss)
        self.create(access="full")
        full = BotToken.objects.get(owner=self.boss).scopes
        self.assertIn("schedule:write", full)
        self.assertNotIn("trading:notes", full)
        BotToken.objects.all().delete()
        self.client.post(reverse("labs:api_keys"), {"name": "Grok", "access": "custom", "scopes": ["trading:notes"],
                                                   "password": PASSWORD})
        self.assertEqual(BotToken.objects.get(owner=self.boss).scopes, ["trading:notes"])

    def test_edit_access_switches_between_choices(self):
        self.create(access="read")
        token = BotToken.objects.get(owner=self.kim)
        self.client.post(reverse("labs:api_key_scopes", args=[token.pk]), {"access": "full", "password": PASSWORD})
        token.refresh_from_db()
        self.assertIn("schedule:write", token.scopes)
        page = self.client.get(reverse("labs:api_keys")).content.decode()
        self.assertIn("Full access", page.split('id="my-keys-table"', 1)[1])


class LifecycleTests(Base):
    def test_shown_once_with_the_connect_tabs_and_its_access(self):
        response, raw = self.create(name="Kim's Claude", access="read")
        self.assertTrue(raw.startswith("abt_"))
        page = response.content.decode()
        self.assertIn(raw, page)
        self.assertIn("won't be shown again", page.replace("&#x27;", "'"))
        self.assertLess(page.index('id="new-key"'), page.index('id="connect"'))     # the tabs come right after
        self.assertIn("can: Read only", page)
        self.assertNotIn(raw, self.client.get(reverse("labs:api_keys")).content.decode())

    def test_list_shows_last_used_and_expiry(self):
        _response, raw = self.create(expiry="30")
        token = BotToken.objects.get(owner=self.kim)
        self.assertAlmostEqual((token.expires_at - timezone.now()).days, 29, delta=1)
        page = self.client.get(reverse("labs:api_keys")).content.decode()
        self.assertIn("never</td>", page.split('class="ak-last-used"', 1)[1][:40] + "</td>")
        self.get("me", raw)
        row = self.client.get(reverse("labs:api_keys")).content.decode().split('class="ak-last-used"', 1)[1][:200]
        self.assertIn(" ago</time>", row)

    def test_rename_and_revoke(self):
        self.create()
        token = BotToken.objects.get(owner=self.kim)
        self.client.post(reverse("labs:api_key_rename", args=[token.pk]), {"name": "Kitchen tablet"})
        token.refresh_from_db()
        self.assertEqual(token.name, "Kitchen tablet")
        page = self.client.get(reverse("labs:api_keys")).content.decode()
        self.assertIn('data-confirm="Revoke Kitchen tablet?', page)
        self.client.post(reverse("labs:api_key_revoke", args=[token.pk]))
        token.refresh_from_db()
        self.assertFalse(token.is_active)
        self.assertNotIn("Kitchen tablet", self.client.get(reverse("labs:api_keys")).content.decode().split('id="my-keys"', 1)[1].split('id="my-key-activity"', 1)[0])

    def test_expired_keys_get_401(self):
        token, raw = BotToken.issue(self.kim, "Short", ["notes:read"], expires_at=timezone.now() + timedelta(days=1))
        self.assertEqual(self.get("notes", raw).status_code, 200)
        BotToken.objects.filter(pk=token.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        response = self.get("notes", raw)
        self.assertEqual((response.status_code, response.json()["error"]), (401, "This key has expired."))
