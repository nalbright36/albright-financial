"""Account-level pages and Home never render as an app (albright_trading_app.context_processors.app_key_for): a neutral
"Account" chip with the user icon, the Labs accent, the Account sub-nav - and no app is ever the default."""
from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import resolve, reverse

from albright_labs import apps_registry
from albright_labs.testing import with_apps
from albright_trading_app.context_processors import app_key_for

ACCOUNT_URLS = ("labs:account", "labs:api_keys", "labs:circles", "labs:people", "labs:sharing_defaults",
                "labs:shared_with_me", "labs:activity", "labs:notification_settings", "assistant:setup")
# Each app's own nav, as it appears in the header - none may show on an account page or Home.
APP_NAV = ("Grok Bot", "Options Strategies", "Watchlist", "This week", "Groceries", "Pipeline", "Transactions",
           "Programs", "System health")


def header(content):
    """The page from <body> up to the launcher panel: the logo chip, the app's (or account's) nav, the menus."""
    body = content.split("<body", 1)[1]
    return body.split('id="app-launcher-panel"', 1)[0]


class AccountContextTests(TestCase):
    def setUp(self):
        self.boss = with_apps(User.objects.create_superuser("boss", "", "pw-not-real-12345"))
        self.client.force_login(self.boss)

    def check_page(self, url, expect):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, url)
        content = response.content.decode()
        top = header(content)
        self.assertIn(f'<body data-app="{expect}">', content)
        self.assertNotIn("Trading", top)
        self.assertNotIn("Albright Trading", content.split("</title>", 1)[0])
        for label in APP_NAV:
            self.assertNotIn(f"{label}</a>", top, f"{url}: {label}")
        return top

    def test_account_pages_are_neutral_whether_or_not_trading_is_archived(self):
        for archived in ([], ["trading", "reselling"]):
            with override_settings(ARCHIVED_APPS=archived):
                for name in ACCOUNT_URLS:
                    with self.subTest(page=name, archived=archived):
                        top = self.check_page(reverse(name), "account")
                        self.assertIn('data-app-key="account"', self.client.get(reverse(name)).content.decode())
                        self.assertIn("<span>Account</span>", top)             # the neutral chip
                        for link in ("My account", "API keys", "Circles", "People", "Notifications", "Storage",
                                     "Your AI assistant"):
                            self.assertIn(link, top)
                with self.subTest(page="home", archived=archived):
                    self.check_page(reverse("labs:hub"), "labs")

    def test_every_account_url_is_an_account_page(self):
        for name in ACCOUNT_URLS:
            match = resolve(reverse(name))
            self.assertTrue(apps_registry.is_account_page(match), name)
            self.assertEqual(app_key_for(match.app_name, match), "account")

    def test_no_app_is_ever_the_default(self):
        self.assertFalse(hasattr(apps_registry, "DEFAULT_APP"))
        for app_name in (None, "", "admin", "bot_api_v1", "something_new"):
            self.assertEqual(app_key_for(app_name), apps_registry.HOME_ACCENT)
        # The trading app's own pages (no URL namespace) are still Trading - found by their package.
        match = resolve(reverse("strategies"))
        self.assertEqual(app_key_for(match.app_name, match), "trading")
        match = resolve(reverse("user_login"))                      # its sign-in page belongs to no app
        self.assertEqual(app_key_for(match.app_name, match), apps_registry.HOME_ACCENT)
