"""The header nav: Admin's Overview + Bot / System / Site groups with the
right links, the open page's group marked, attention badges; the account
menu (My account, Logout) far right in every app; and the shared rules -
the brand never wraps or shrinks, the nav collapses into "Menu" instead of
wrapping, and dropdowns carry the ARIA a keyboard/screen reader needs."""
import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotToken, SiteSuggestion
from albright_control import nav
from albright_control.models import Idea, JobRun
from albright_labs.testing import with_apps

PASSWORD = "pw-not-real-12345"
OUTLOOK = "albright_trading_app.views.get_market_outlook"
STYLE = Path(settings.BASE_DIR) / "albright_trading_app" / "static" / "style.css"


def links_html(content):
    return content.split('id="navbar-links"', 1)[1].split('class="navbar__dropdown navbar__account"', 1)[0]


def group_html(content, key):
    return content.split(f'data-nav-group="{key}"', 1)[1].split("</div>\n                        </div>", 1)[0]


def hrefs(html):
    return [(href, label) for href, label in re.findall(r'<a href="([^"]+)"[^>]*>(?:<span>)?([^<]+)<', html)]


def account_menu(content):
    return content.split('id="account-menu"', 1)[1].split("</div>", 1)[0]


class Base(TestCase):
    def setUp(self):
        cache.delete(nav.HEALTH_CACHE_KEY)
        self.outlook = mock.patch(OUTLOOK, return_value=None)
        self.outlook.start()
        self.addCleanup(self.outlook.stop)
        self.admin = User.objects.create_superuser(username="boss", password=PASSWORD, email="")
        self.client.force_login(self.admin)

    def page(self, url):
        return self.client.get(url).content.decode()


class AdminGroupTests(Base):
    def test_overview_then_three_groups_with_the_right_links(self):
        content = self.page(reverse("albright_control:overview"))
        links = links_html(content)
        top = re.findall(r'<a href="([^"]+)"[^>]*>Overview</a>|data-nav-group="(\w+)"', links)
        self.assertEqual([href or key for href, key in top],
                         [reverse("albright_control:overview"), "bot", "system", "site"])
        expected = {
            "bot": [("albright_control:bot_api", "Bot API"), ("albright_control:suggestions", "Bot suggestions"),
                    ("albright_control:ideas", "Ideas")],
            "system": [("albright_control:health", "System health"), ("albright_control:integrations", "Integrations"),
                       ("albright_control:ai_usage", "AI usage"), ("albright_control:data", "Data"),
                       ("albright_control:audit", "Audit log")],
            "site": [("albright_resume_admin:overview", "Resume"), ("labs_admin:settings", "Home page"),
                     ("albright_control:users", "Users")],
        }
        for key, items in expected.items():
            with self.subTest(group=key):
                html = group_html(content, key)
                self.assertEqual(hrefs(html), [(reverse(name), label) for name, label in items])
                self.assertIn(f'aria-haspopup="true" aria-expanded="false" aria-controls="admin-{key}-menu"', html)
                self.assertIn(f'id="admin-{key}-menu"', html)
        # 4 top-level entries instead of 14 links.
        self.assertEqual(links.count('class="navbar__dropdown-trigger"'), 3)

    def test_the_open_pages_group_is_marked(self):
        cases = {
            reverse("albright_control:ideas"): ("bot", "Ideas"),
            reverse("albright_control:bot_activity"): ("bot", "Bot API"),
            reverse("albright_control:health"): ("system", "System health"),
            reverse("albright_control:audit"): ("system", "Audit log"),
            reverse("albright_resume_admin:conversations"): ("site", "Resume"),
            reverse("labs_admin:settings"): ("site", "Home page"),
            reverse("albright_control:users"): ("site", "Users"),
        }
        for url, (group, item) in cases.items():
            with self.subTest(url=url):
                content = self.page(url)
                marked = re.findall(r'class="navbar__dropdown is-active" data-nav-group="(\w+)"', content)
                self.assertEqual(marked, [group])
                self.assertIn(f'class="active" aria-current="page"><span>{item}</span>', group_html(content, group))
                self.assertIn("(current section)", group_html(content, group))
                self.assertNotIn('class="active" aria-current="page">Overview</a>', content)
        overview = self.page(reverse("albright_control:overview"))
        self.assertIn('class="active" aria-current="page">Overview</a>', overview)
        self.assertNotIn("navbar__dropdown is-active", overview)

    def test_attention_badges(self):
        content = self.page(reverse("albright_control:overview"))
        self.assertNotIn("navbar__badge", group_html(content, "bot"))               # nothing new yet
        self.assertNotIn("navbar__badge", group_html(content, "site"))
        token, _raw = BotToken.issue(self.admin, "Grok bot", ["site:suggest"])
        SiteSuggestion.objects.create(token=token, app="opps", title="Calendar view", description="Deadlines.")
        Idea.objects.create(title="Sound alert", original_message="beep")
        Idea.objects.create(title="Done one", original_message="x", status="done")
        content = self.page(reverse("albright_control:overview"))
        bot = group_html(content, "bot")
        self.assertIn('<span class="badge warn navbar__badge">2<span class="visually-hidden"> need attention</span>', bot)
        self.assertIn('<span>Bot suggestions</span> <span class="badge warn navbar__badge">1</span>', bot)
        self.assertIn('<span>Ideas</span> <span class="badge warn navbar__badge">1</span>', bot)

    def test_failing_jobs_badge_the_system_group(self):
        JobRun.objects.create(job="alert_digest", ok=False, error="Telegram down", finished_at=timezone.now())
        content = self.page(reverse("albright_control:overview"))
        system = group_html(content, "system")
        self.assertRegex(system, r'<span class="badge loss navbar__badge">\d+<span class="visually-hidden">')
        self.assertRegex(system, r'<span>System health</span> <span class="badge loss navbar__badge">\d+</span>')

    def test_groups_only_in_the_admin_area(self):
        self.assertNotIn("data-nav-group", self.page(reverse("trading_home")))
        self.assertNotIn("data-nav-group", self.page(reverse("albright_reselling_app:dashboard")))


class AccountMenuTests(Base):
    def test_every_app_has_the_account_menu_with_my_account_and_logout(self):
        regular = with_apps(User.objects.create_user(username="pat", password=PASSWORD))
        for user in (self.admin, regular):
            self.client.force_login(user)
            urls = [reverse("labs:hub"), reverse("trading_home"), reverse("albright_reselling_app:dashboard"),
                    reverse("albright_crypto_app:inbox"), reverse("labs:account")]
            if user.is_superuser:
                urls += [reverse("albright_control:overview"), reverse("albright_resume_admin:overview")]
            for url in urls:
                with self.subTest(user=user.username, url=url):
                    content = self.page(url)
                    self.assertEqual(hrefs(account_menu(content)),
                                     [(reverse("labs:account"), "My account"), (reverse("user_logout"), "Logout")])
                    self.assertIn('aria-haspopup="true" aria-expanded="false" aria-controls="account-menu"', content)
                    self.assertIn(f'title="Signed in as {user.username}"', content)
                    links = links_html(content)
                    self.assertNotIn(">My account<", links)                       # not in the app's own links
                    self.assertNotIn(">Logout<", links)
                    # Far right: after the app's links.
                    self.assertGreater(content.index('id="account-menu"'), content.index('id="navbar-links"'))

    def test_visitors_get_login_only(self):
        self.client.logout()
        content = self.page(reverse("user_login"))
        self.assertNotIn('id="account-menu"', content)
        links = content.split('id="navbar-links"', 1)[1].split("</div>", 1)[0]
        self.assertEqual(hrefs(links), [(reverse("user_login"), "Login")])        # accounts are invite-only


class SharedHeaderRuleTests(Base):
    def test_menu_toggle_is_a_real_button_with_aria(self):
        content = self.page(reverse("albright_control:overview"))
        self.assertIn('<button type="button" class="navbar__toggle" id="nav-toggle" aria-haspopup="true" '
                      'aria-expanded="false" aria-controls="navbar-links" aria-label="Menu">', content)
        self.assertIn('class="navbar__links" id="navbar-links"', content)
        self.assertNotIn('type="checkbox" id="nav-toggle"', content)

    def test_every_dropdown_trigger_is_a_button_with_aria(self):
        for url in (reverse("trading_home"), reverse("albright_reselling_app:dashboard"),
                    reverse("albright_control:health")):
            with self.subTest(url=url):
                content = self.page(url)
                triggers = re.findall(r'<(\w+) type="button" class="navbar__dropdown-trigger[^"]*" ([^>]*)>', content)
                self.assertTrue(triggers)
                self.assertEqual(content.count('class="navbar__dropdown-trigger'), len(triggers))
                for tag, attrs in triggers:
                    self.assertEqual(tag, "button")
                    self.assertIn('aria-haspopup="true" aria-expanded="false" aria-controls="', attrs)
                for menu_id in re.findall(r'aria-controls="([\w-]+-menu)"', content):
                    self.assertIn(f'id="{menu_id}"', content)

    def test_brand_never_wraps_and_nav_collapses_instead(self):
        css = STYLE.read_text(encoding="utf-8")
        brand = css.split("\n.navbar__brand {", 1)[1].split("}", 1)[0]
        self.assertIn("white-space: nowrap;", brand)
        self.assertIn("flex-shrink: 0;", brand)
        links = css.split("\n.navbar__links {", 1)[1].split("}", 1)[0]
        self.assertIn("flex-wrap: nowrap;", links)
        self.assertIn(".navbar--collapsed .navbar__toggle { display: inline-flex; }", css)
        self.assertIn(".navbar--collapsed.navbar--menu-open .navbar__links { display: flex; }", css)
        # Click to open (hover only without JS).
        self.assertNotIn("\n.navbar__dropdown:hover .navbar__dropdown-menu", css)
        self.assertIn("html:not(.js) .navbar__dropdown:hover .navbar__dropdown-menu", css)

    def test_the_fit_check_and_keyboard_handling_are_on_every_page(self):
        content = self.page(reverse("trading_home"))
        for snippet in ("row.scrollWidth > row.clientWidth", "matchMedia('(max-width: 640px)')",
                        "e.key === 'ArrowDown'", "e.key === 'Escape'", "'focusout'",
                        "document.documentElement.classList.add('js')"):
            self.assertIn(snippet, content)
