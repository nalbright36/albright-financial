"""App access and navigation: the registry drives the header, Home's tiles
and the "where I land" options (a new app is one entry); access is enforced
with a 404 on page and JSON views; superusers always have everything;
hiding an app changes only the menu, not access; at least one app stays
visible; the user's order is respected; at 5+ buttons the extras go in
"More"; the landing preference falls back to Home when its app is hidden or
access is removed; and Admin > Users is superuser-only."""
import dataclasses
import re
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from albright_control.models import AuditEntry
from albright_trading_proj.middleware import LAST_APP_COOKIE

from . import apps_registry
from .models import UserPreference
from .tests import OUTLOOK, resume_site

PASSWORD = "pw-not-real-12345"


def switch_row(content):
    return content.split('class="navbar__switch-row"')[1].split("</div>")[0]


def buttons(content):
    """[(data-app-target, label), ...] of the header's app buttons, inline and in "More"."""
    return re.findall(r'class="app-switch-btn[^"]*" data-app-target="(\w+)"[^>]*>([^<]+)</a>', switch_row(content))


def inline_buttons(content):
    return buttons(content.split('id="app-switch-more"')[0] + "</div>")


def more_buttons(content):
    if 'id="app-switch-more"' not in content:
        return []
    return buttons('class="navbar__switch-row"' + content.split('id="app-switch-more"')[1])


class Base(TestCase):
    def setUp(self):
        self.outlook = mock.patch(OUTLOOK, return_value=None)
        self.outlook.start()
        self.addCleanup(self.outlook.stop)
        self.user = User.objects.create_user(username="pat", password=PASSWORD)
        self.client.force_login(self.user)

    def set_pref(self, **fields):
        pref = UserPreference.for_user(self.user)
        for name, value in fields.items():
            setattr(pref, name, value)
        pref.save()
        return pref

    def page(self, url):
        return self.client.get(url).content.decode()


class RegistryTests(Base):
    MEALS = apps_registry.App(
        key="meals", label="Meals", short_label="Meals", brand="Albright Meals", accent="meals", color="#F59E0B",
        landing_url_name="labs:account", namespaces=("meals_test",), description="Plan the week's meals.",
        last_app_cookie="meals", pages=(("meals:plan", "Plan", "labs:account"),))

    def with_meals(self):
        apps = apps_registry.APPS[:3] + (self.MEALS,) + apps_registry.APPS[3:]
        patches = [mock.patch.object(apps_registry, "APPS", apps),
                   mock.patch.object(apps_registry, "BY_KEY", {a.key: a for a in apps}),
                   mock.patch.object(apps_registry, "BY_NAMESPACE", {ns: a for a in apps for ns in a.namespaces}),
                   mock.patch.object(apps_registry, "BY_COOKIE", {a.last_app_cookie: a for a in apps}),
                   mock.patch.object(apps_registry, "PAGES", {k: (a, label, url) for a in apps
                                                              for k, label, url in a.pages})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_header_tiles_and_landing_options_come_from_the_registry(self):
        hub = self.page(reverse("labs:hub"))
        self.assertEqual(buttons(hub), [("labs", "Home")] + [(a.accent, a.short_label) for a in apps_registry.APPS
                                                      if not a.superuser_only])
        self.assertEqual(re.findall(r'data-tile="(\w+)"', hub), ["trade", "resell", "opps"])
        options = self.client.get(reverse("labs:hub")).context["landing_form"].fields["landing_page"].choices
        self.assertEqual([group for group, _ in options[1:]], ["Trade", "Resell", "Opps"])
        self.assertEqual([k for k, _ in options[2][1]], [k for k, _l, _u in apps_registry.BY_KEY["resell"].pages])

    def test_a_new_app_is_one_entry(self):
        self.with_meals()
        hub = self.page(reverse("labs:hub"))
        # Home + four apps = five buttons: Home + three on the row, the fourth in "More".
        self.assertEqual(inline_buttons(hub), [("labs", "Home"), ("trading", "Trade"), ("reselling", "Resell"),
                                               ("opportunities", "Opps")])
        self.assertEqual(more_buttons(hub), [("meals", "Meals")])
        self.assertIn('style="--app-dot: var(--app-meals, #F59E0B);"', hub)
        self.assertIn('data-tile="meals"', hub)
        self.assertIn("Plan the week&#x27;s meals.", hub)
        choices = dict(self.client.get(reverse("labs:hub")).context["landing_form"].fields["landing_page"].choices)
        self.assertEqual(choices["Meals"], [("meals:plan", "Plan")])
        self.client.post(reverse("labs:save_landing"), {"landing": "page", "landing_page": "meals:plan"})
        self.assertRedirects(self.client.get("/"), reverse("labs:account"), fetch_redirect_response=False)
        # Admin > Users offers it too.
        self.user.is_superuser = True
        self.user.save()
        self.assertIn('value="meals"', self.page(reverse("albright_control:users")))


class AccessTests(Base):
    def setUp(self):
        super().setUp()
        self.set_pref(allowed_apps=["resell"])

    def test_pages_of_an_app_without_access_are_404(self):
        for url in (reverse("trading_home"), reverse("grok_bot"), reverse("strategies"),
                    reverse("albright_crypto_app:inbox"), reverse("albright_crypto_app:pipeline"),
                    reverse("albright_crypto_app:settings")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.get(reverse("albright_reselling_app:dashboard")).status_code, 200)

    def test_json_and_post_endpoints_are_404_too(self):
        self.assertEqual(self.client.get(reverse("stock_bars_api", args=["AAPL"]),
                                         HTTP_X_REQUESTED_WITH="XMLHttpRequest").status_code, 404)
        self.assertEqual(self.client.post(reverse("albright_crypto_app:inbox_keep", args=[1])).status_code, 404)
        self.assertEqual(self.client.post(reverse("strategy_toggle", args=[1])).status_code, 404)
        self.assertEqual(self.client.get(reverse("labs:tile", args=["trade"])).status_code, 404)
        self.assertEqual(self.client.get(reverse("labs:tile", args=["opps"])).status_code, 404)
        allowed = self.client.get(reverse("albright_reselling_app:watchlist_summary"))
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(self.client.get(reverse("labs:tile", args=["resell"])).status_code, 200)

    def test_no_redirect_gives_the_app_away(self):
        response = self.client.get(reverse("albright_crypto_app:old_crypto"))   # normally a redirect to the Inbox
        self.assertEqual(response.status_code, 404)
        self.assertNotIn("Opportunities", response.content.decode())

    def test_header_and_home_show_only_allowed_apps(self):
        hub = self.page(reverse("labs:hub"))
        self.assertEqual(buttons(hub), [("labs", "Home"), ("reselling", "Resell")])
        self.assertEqual(re.findall(r'data-tile="(\w+)"', hub), ["resell"])
        self.assertNotIn("Trade", [label for _, label in buttons(hub)])

    def test_public_pages_bot_api_and_sign_in_are_unaffected(self):
        resume_site()
        for url in ("/", reverse("albright_resume:page"), reverse("labs:hub"), reverse("labs:account"),
                    reverse("user_logout")):
            with self.subTest(url=url):
                self.assertNotEqual(self.client.get(url).status_code, 404)
        full = User.objects.create_user(username="full", password=PASSWORD)
        docs = reverse("bot_api_v1:docs")
        restricted_status = self.client.get(docs).status_code
        self.client.force_login(full)
        self.assertEqual(self.client.get(docs).status_code, restricted_status)
        self.assertNotEqual(restricted_status, 404)
        self.client.logout()
        self.assertEqual(self.client.get(reverse("user_login")).status_code, 200)

    def test_visitors_still_get_the_sign_in_redirect(self):
        self.client.logout()
        response = self.client.get(reverse("albright_crypto_app:inbox"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("next=/opportunities/", response["Location"])

    def test_no_apps_at_all(self):
        self.set_pref(allowed_apps=[])
        hub = self.page(reverse("labs:hub"))
        self.assertEqual(buttons(hub), [("labs", "Home")])
        self.assertIn("You don't have access to any apps yet", hub)
        self.assertEqual(self.client.get(reverse("albright_reselling_app:dashboard")).status_code, 404)


class SuperuserTests(Base):
    def test_superusers_always_have_everything(self):
        self.user.is_superuser = True
        self.user.save()
        self.set_pref(allowed_apps=[])
        for url in (reverse("trading_home"), reverse("albright_reselling_app:dashboard"),
                    reverse("albright_crypto_app:inbox"), reverse("albright_control:overview")):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)
        hub = self.page(reverse("labs:hub"))
        self.assertEqual(re.findall(r'data-tile="(\w+)"', hub), ["trade", "resell", "opps", "admin"])
        # Home + four apps: Admin moves into "More", and is highlighted there in the Admin area.
        self.assertEqual([label for _, label in inline_buttons(hub)], ["Home", "Trade", "Resell", "Opps"])
        self.assertEqual(more_buttons(hub), [("admin", "Admin")])
        admin = self.page(reverse("albright_control:overview"))
        self.assertIn('app-switch-more__toggle is-current', admin)
        self.assertIn('data-app-target="admin" title="Albright Admin" aria-current="page"', admin)

    def test_regular_users_three_apps_fit_without_more(self):
        self.assertNotIn('id="app-switch-more"', self.page(reverse("labs:hub")))


class VisibilityTests(Base):
    def test_hiding_changes_only_the_menu(self):
        self.client.post(reverse("labs:save_nav"), {"visible": ["resell"], "order": ["trade", "resell", "opps"]})
        hub = self.page(reverse("labs:hub"))
        self.assertEqual(buttons(hub), [("labs", "Home"), ("reselling", "Resell")])
        self.assertEqual(re.findall(r'data-tile="(\w+)"', hub), ["resell"])
        self.assertNotIn('id="nav-hidden-note"', hub)
        trade = self.client.get(reverse("trading_home"))
        self.assertEqual(trade.status_code, 200)                                  # still opens by URL
        self.assertIn("Hidden from your nav", trade.content.decode())
        self.assertIn(reverse("labs:show_app", args=["trade"]), trade.content.decode())
        self.assertEqual(self.client.get(reverse("labs:tile", args=["trade"])).status_code, 200)
        self.assertNotIn("Hidden from your nav", self.page(reverse("albright_reselling_app:dashboard")))

    def test_show_it(self):
        self.set_pref(visible_apps=["resell"])
        response = self.client.post(reverse("labs:show_app", args=["opps"]),
                                    {"next": reverse("albright_crypto_app:pipeline")})
        self.assertRedirects(response, reverse("albright_crypto_app:pipeline"), fetch_redirect_response=False)
        self.assertEqual([label for _, label in buttons(self.page(reverse("labs:hub")))], ["Home", "Resell", "Opps"])
        self.assertEqual(self.client.post(reverse("labs:show_app", args=["opps"]),
                                          {"next": "https://evil.example.com/"})["Location"],
                         reverse("albright_crypto_app:inbox"))                    # off-site next ignored
        self.set_pref(allowed_apps=["resell"])
        self.assertEqual(self.client.post(reverse("labs:show_app", args=["opps"])).status_code, 404)

    def test_at_least_one_app_stays_visible(self):
        response = self.client.post(reverse("labs:save_nav"), {"order": ["trade", "resell", "opps"]}, follow=True)
        self.assertContains(response, "Keep at least one app in your navigation.")
        self.assertEqual(len(buttons(self.page(reverse("labs:hub")))), 4)        # unchanged
        # Access removed from the only shown app: the first remaining one still shows.
        self.set_pref(visible_apps=["opps"], allowed_apps=["trade", "resell"])
        self.assertEqual(buttons(self.page(reverse("labs:hub"))), [("labs", "Home"), ("trading", "Trade")])

    def test_home_is_always_shown(self):
        self.client.post(reverse("labs:save_nav"), {"visible": ["opps"], "order": ["opps"]})
        self.assertEqual(buttons(self.page(reverse("albright_crypto_app:inbox")))[0], ("labs", "Home"))

    def test_my_account_page_is_linked_from_the_header(self):
        hub = self.page(reverse("labs:hub"))
        self.assertIn(f'<a href="{reverse("labs:account")}" id="nav-account-link">My account</a>', hub)
        account = self.client.get(reverse("labs:account"))
        self.assertTemplateUsed(account, "albright_labs/_app_settings.html")
        self.assertContains(account, 'id="nav-form"')
        self.assertContains(account, 'id="landing-form"')
        response = self.client.post(reverse("labs:save_nav"), {"visible": ["trade"], "order": ["trade"],
                                                               "next": reverse("labs:account")})
        self.assertRedirects(response, reverse("labs:account"), fetch_redirect_response=False)

    def test_only_allowed_apps_are_offered(self):
        self.set_pref(allowed_apps=["trade", "opps"])
        rows = self.client.get(reverse("labs:account")).context["nav_rows"]
        self.assertEqual([r["key"] for r in rows], ["trade", "opps"])
        self.client.post(reverse("labs:save_nav"), {"visible": ["trade", "resell"], "order": ["resell", "trade"]})
        self.assertEqual(buttons(self.page(reverse("labs:hub"))), [("labs", "Home"), ("trading", "Trade")])
        self.assertEqual(self.client.get(reverse("albright_reselling_app:dashboard")).status_code, 404)


class OrderTests(Base):
    def test_order_is_respected_in_the_header_and_on_home(self):
        self.client.post(reverse("labs:save_nav"), {"visible": ["trade", "resell", "opps"],
                                                    "order": ["opps", "trade", "resell"]})
        hub = self.page(reverse("labs:hub"))
        self.assertEqual([label for _, label in buttons(hub)], ["Home", "Opps", "Trade", "Resell"])
        self.assertEqual(re.findall(r'data-tile="(\w+)"', hub), ["opps", "trade", "resell"])
        self.assertEqual([r["key"] for r in self.client.get(reverse("labs:account")).context["nav_rows"]],
                         ["opps", "trade", "resell"])
        options = self.client.get(reverse("labs:hub")).context["landing_form"].fields["landing_page"].choices
        self.assertEqual([group for group, _ in options[1:]], ["Opps", "Trade", "Resell"])

    def test_up_down_buttons_work_without_js(self):
        order = ["trade", "resell", "opps"]
        self.client.post(reverse("labs:save_nav"), {"visible": order, "order": order, "move": "up:opps"})
        self.assertEqual(UserPreference.objects.get(user=self.user).app_order[:3], ["trade", "opps", "resell"])
        self.client.post(reverse("labs:save_nav"), {"visible": order, "order": ["trade", "opps", "resell"],
                                                    "move": "down:trade"})
        self.assertEqual(UserPreference.objects.get(user=self.user).app_order[:3], ["opps", "trade", "resell"])
        self.client.post(reverse("labs:save_nav"), {"visible": order, "order": ["opps", "trade", "resell"],
                                                    "move": "up:opps"})              # already first: no change
        self.assertEqual(UserPreference.objects.get(user=self.user).app_order[:3], ["opps", "trade", "resell"])

    def test_a_newly_granted_app_goes_at_the_end_and_shows(self):
        self.set_pref(allowed_apps=["opps"], visible_apps=["opps"], app_order=["opps"])
        superuser = User.objects.create_superuser(username="boss", password=PASSWORD, email="")
        self.client.force_login(superuser)
        self.client.post(reverse("albright_control:users"), {"action": "access", "user_id": self.user.pk,
                                                            "apps": ["opps", "trade"]})
        self.client.force_login(self.user)
        self.assertEqual([label for _, label in buttons(self.page(reverse("labs:hub")))], ["Home", "Opps", "Trade"])


class LandingFallbackTests(Base):
    def sign_in(self):
        self.client.logout()
        return self.client.post(reverse("user_login"), {"username": "pat", "password": PASSWORD})

    def test_hidden_app_falls_back_to_home(self):
        self.set_pref(landing="page", landing_page="opps:inbox")
        self.assertRedirects(self.client.get("/"), reverse("albright_crypto_app:inbox"), fetch_redirect_response=False)
        self.set_pref(visible_apps=["trade", "resell"])
        self.sign_in()
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")
        choices = dict(self.client.get(reverse("labs:hub")).context["landing_form"].fields["landing_page"].choices)
        self.assertNotIn("Opps", choices)

    def test_removed_access_falls_back_to_home(self):
        self.set_pref(landing="page", landing_page="resell:watchlist", allowed_apps=["trade", "opps"])
        self.sign_in()
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")

    def test_last_used_app_hidden_or_removed_falls_back_to_home(self):
        self.set_pref(landing="last_app")
        self.client.cookies[LAST_APP_COOKIE] = "reseller"
        self.assertRedirects(self.client.get("/"), reverse("albright_reselling_app:dashboard"),
                             fetch_redirect_response=False)
        self.set_pref(visible_apps=["trade"])
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")
        self.set_pref(visible_apps=["trade", "resell"], allowed_apps=["trade"])
        self.assertTemplateUsed(self.client.get("/"), "albright_labs/hub.html")

    def test_a_hidden_apps_page_cant_be_chosen(self):
        self.set_pref(visible_apps=["trade"])
        self.client.post(reverse("labs:save_landing"), {"landing": "page", "landing_page": "resell:watchlist"})
        self.assertEqual(UserPreference.objects.get(user=self.user).landing_page, "")


class AdminUsersTests(Base):
    def test_superuser_only(self):
        url = reverse("albright_control:users")
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.client.post(url, {"action": "access", "user_id": self.user.pk}).status_code, 404)
        self.client.logout()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.client.force_login(User.objects.create_superuser(username="boss", password=PASSWORD, email=""))
        page = self.client.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Superuser - every app")
        self.assertIn(f'<a href="{url}">Users</a>', page.content.decode())         # in the Admin nav

    def test_changing_access(self):
        self.client.force_login(User.objects.create_superuser(username="boss", password=PASSWORD, email=""))
        self.client.post(reverse("albright_control:users"), {"action": "access", "user_id": self.user.pk,
                                                            "apps": ["resell", "admin", "bogus"]})
        self.assertEqual(UserPreference.objects.get(user=self.user).allowed_apps, ["resell"])   # Admin can't be granted
        self.assertTrue(AuditEntry.objects.filter(action="user_access_changed").exists())
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("trading_home")).status_code, 404)
        self.assertEqual(self.client.get(reverse("albright_control:overview")).status_code, 404)

    def test_creating_a_user_with_their_apps(self):
        self.client.force_login(User.objects.create_superuser(username="boss", password=PASSWORD, email=""))
        response = self.client.post(reverse("albright_control:users"), {
            "action": "create", "username": "newbie", "email": "", "password": "a-Long-unusual-pass-9481",
            "apps": ["opps"]})
        self.assertRedirects(response, reverse("albright_control:users"), fetch_redirect_response=False)
        newbie = User.objects.get(username="newbie")
        self.assertFalse(newbie.is_superuser)
        self.assertEqual(UserPreference.objects.get(user=newbie).allowed_apps, ["opps"])
        entry = AuditEntry.objects.get(action="user_created")
        self.assertNotIn("a-Long-unusual-pass-9481", entry.details)
        self.client.force_login(newbie)
        self.assertEqual(self.client.get(reverse("albright_crypto_app:inbox")).status_code, 200)
        self.assertEqual(self.client.get(reverse("albright_reselling_app:dashboard")).status_code, 404)

    def test_create_form_validation(self):
        self.client.force_login(User.objects.create_superuser(username="boss", password=PASSWORD, email=""))
        response = self.client.post(reverse("albright_control:users"), {
            "action": "create", "username": "PAT", "password": "123", "apps": ["trade"]})
        self.assertContains(response, "That username is taken.")
        self.assertNotContains(response, 'value="123"')                             # never echoed back
        self.assertFalse(User.objects.filter(username="PAT").exists())

    def test_superuser_rows_are_read_only(self):
        boss = User.objects.create_superuser(username="boss", password=PASSWORD, email="")
        self.client.force_login(boss)
        self.client.post(reverse("albright_control:users"), {"action": "access", "user_id": boss.pk, "apps": []})
        self.assertFalse(UserPreference.objects.filter(user=boss).exists())


class ExistingUsersTests(Base):
    def test_existing_users_see_what_they_saw(self):
        """A user with no preference row, or one saved before these fields: every app, in the old order."""
        hub = self.page(reverse("labs:hub"))
        self.assertEqual([label for _, label in buttons(hub)], ["Home", "Trade", "Resell", "Opps"])
        self.set_pref(landing="page", landing_page="resell:watchlist")
        pref = UserPreference.objects.get(user=self.user)
        self.assertEqual((pref.allowed_apps, pref.visible_apps, pref.app_order),
                         (["trade", "resell", "opps"], ["trade", "resell", "opps", "admin"],
                          ["trade", "resell", "opps", "admin"]))

    def test_the_registry_entries_are_complete(self):
        for app in apps_registry.APPS:
            with self.subTest(app=app.key):
                self.assertTrue(reverse(app.landing_url_name))
                for _key, _label, url_name in app.pages:
                    self.assertTrue(reverse(url_name))
                self.assertTrue(all(dataclasses.asdict(app)[f] for f in ("label", "short_label", "brand",
                                                                         "accent", "color", "description")))
