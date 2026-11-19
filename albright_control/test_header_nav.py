"""The header nav: Admin's Overview + Bot / System / Site groups with the
right links, the open page's group marked, attention badges; the account
menu (My account, Circles, Logout) far right in every app; and the shared rules -
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
from albright_labs.templatetags import nav_icons
from albright_labs.testing import with_apps

PASSWORD = "pw-not-real-12345"
OUTLOOK = "albright_trading_app.views.get_market_outlook"
STYLE = Path(settings.BASE_DIR) / "albright_trading_app" / "static" / "style.css"


def links_html(content):
    """The app's own links - not the phone-only theme/account section at the bottom of the panel."""
    return content.split('id="navbar-links"', 1)[1].split('id="navbar-menu-extra"', 1)[0]


def menu_extra(content):
    return content.split('id="navbar-menu-extra"', 1)[1].split('<div class="navbar__tools">', 1)[0]


def group_html(content, key):
    return content.split(f'data-nav-group="{key}"', 1)[1].split("</div>\n                        </div>", 1)[0]


ICON = re.compile(r'<svg class="navbar__icon[^"]*".*?</svg>|<span class="navbar__icon" aria-hidden="true"></span>', re.S)


def hrefs(html):
    """(href, label) of each link - its phone-menu icon (nav_icons) left out."""
    return [(href, label) for href, label in re.findall(r'<a href="([^"]+)"[^>]*>(?:<span>)?([^<]+)<', ICON.sub("", html))]


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
        """The page without the phone menu's icons (nav_icons) - they sit in front of every label."""
        return ICON.sub("", self.raw(url))

    def raw(self, url):
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
                    ("assistant_admin:overview", "AI assistant"), ("albright_control:ideas", "Ideas")],
            "system": [("albright_control:health", "System health"), ("albright_control:integrations", "Integrations"),
                       ("albright_control:ai_usage", "AI usage"), ("albright_control:data", "Data"),
                       ("albright_control:storage", "Storage"), ("albright_control:audit", "Audit log")],
            "site": [("labs_admin:invite_requests", "Invite requests"),
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
            reverse("labs_admin:invite_requests"): ("site", "Invite requests"),
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
                urls += [reverse("albright_control:overview"), reverse("labs_admin:invite_requests")]
            for url in urls:
                with self.subTest(user=user.username, url=url):
                    content = self.page(url)
                    self.assertEqual(hrefs(account_menu(content)),
                                     [(reverse("labs:account"), "My account"), (reverse("labs:circles"), "Circles"),
                                      (reverse("labs:api_keys"), "API keys"), (reverse("user_logout"), "Logout")])
                    self.assertIn('aria-haspopup="true" aria-expanded="false" aria-controls="account-menu"', content)
                    self.assertIn(f'title="Signed in as {user.username}"', content)
                    links = links_html(content)
                    self.assertNotIn(">My account<", links)                       # not in the app's own links
                    self.assertNotIn(">Logout<", links)
                    # Far right: after the app's links.
                    self.assertGreater(content.index('id="account-menu"'), content.index('id="navbar-links"'))

    def test_visitors_get_no_app_header(self):
        # The sign-in page is a public page (templates/base_public.html): no app header at all.
        self.client.logout()
        content = self.page(reverse("user_login"))
        self.assertNotIn('id="account-menu"', content)
        self.assertNotIn('id="navbar-links"', content)
        self.assertNotIn("navbar__chip", content)


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


class PhoneHeaderTests(Base):
    """Phones (under 640px): one row - logo, the app chip as just its icon, then the bell, the launcher and the
    hamburger; the theme toggle and the account menu's links at the bottom of the hamburger panel."""

    def phone_css(self):
        css = STYLE.read_text(encoding="utf-8")
        start = css.index("/* Phones (under 640px), every app: ONE row")
        return css[start:css.index("@media (max-width: 370px)", start)]

    def test_the_menu_ends_with_the_theme_toggle_and_account_links(self):
        for url in (reverse("labs:hub"), reverse("albright_crypto_app:inbox"), reverse("albright_planner:day"),
                    reverse("albright_control:overview")):
            with self.subTest(url=url):
                content = self.page(url)
                extra = menu_extra(content)
                self.assertIn('id="theme-toggle-menu"', extra)
                self.assertEqual(hrefs(extra), [(reverse("labs:account"), "My account"),
                                                (reverse("labs:circles"), "Circles"),
                                                (reverse("labs:people"), "People"),
                                                (reverse("labs:activity"), "Notifications"),
                                                (reverse("labs:api_keys"), "API keys"),
                                                (reverse("assistant:setup"), "AI assistant"),   # superuser
                                                (reverse("user_logout"), "Logout")])
                # Inside the hamburger panel, after the app's own links; the desktop tools keep theirs.
                links_start = content.index('id="navbar-links"')
                self.assertLess(links_start, content.index('id="navbar-menu-extra"'))
                self.assertLess(content.index('id="navbar-menu-extra"'), content.index('<div class="navbar__tools">'))
                self.assertIn('id="theme-toggle"', content.split('<div class="navbar__tools">', 1)[1])
                self.assertIn('id="account-menu"', content)

    def test_the_phone_menu_has_an_app_section_then_a_separate_account_section(self):
        cases = ((reverse("albright_fitness:programs"), "fitness", "Fitness", "Programs", "clipboard"),
                 (reverse("albright_planner:week"), "planner", "Planner", "Week", "calendar-week"),
                 (reverse("albright_money:budget"), "money", "Money", "Budget", "pie-chart"))
        for url, accent, label, page, icon in cases:
            with self.subTest(url=url):
                content = self.raw(url)
                self.assertIn(f'<body data-app="{accent}">', content)
                links = links_html(content)
                # The app section: its pill first, then its pages - the open one marked, with its icon.
                pill = links.split('<p class="navbar__menu-app"><span class="navbar__menu-app-pill">', 1)[1].split("</p>", 1)[0]
                self.assertIn(f"<span>{label}</span>", pill)
                current = re.findall(r'<a href="[^"]+" class="active" aria-current="page">(.*?)</a>', links, re.S)
                self.assertEqual(len(current), 1)
                self.assertEqual(ICON.sub("", current[0]), page)
                self.assertIn(nav_icons.PATHS[icon], current[0])
                self.assertNotIn("My account", links)
                self.assertNotIn("Logout", links)
                # The account section: labelled with the username, My account first and Logout last, no page marked.
                extra = menu_extra(content)
                self.assertIn('<p class="navbar__menu-label">Account &middot; <span class="navbar__menu-user">boss</span></p>', extra)
                labels = [label for _href, label in hrefs(extra)]
                self.assertEqual((labels[0], labels[-1]), ("My account", "Logout"))
                self.assertNotIn('aria-current="page"', extra)
                self.assertNotIn("Signed in as", extra)
                self.assertIn(nav_icons.PATHS["log-out"], extra)

    def test_phone_menu_css_sections_accent_and_scrolling(self):
        css = self.phone_css()
        for rule in (".navbar--collapsed .navbar__menu-extra { display: block; margin-top: auto;",
                     "background: var(--menu-account-bg);", "overflow-y: auto; overscroll-behavior: contain;",
                     "height: calc(100dvh - var(--navbar-bottom, 56px));",
                     "background: var(--accent-soft); color: var(--accent); box-shadow: inset 3px 0 0 var(--accent);",
                     "font-size: 11px; font-weight: 600; letter-spacing: .09em; text-transform: uppercase;"):
            self.assertIn(rule, css)
        account = css.split(".navbar__menu-label {", 1)[1]
        self.assertNotIn("accent", account)                       # the account section never wears the app's color
        full = STYLE.read_text(encoding="utf-8")
        self.assertIn(":root { --menu-bg: #171C24; --menu-account-bg: #11151B;", full)
        self.assertIn(".navbar__menu-app, .navbar__icon { display: none; }", full)   # wider screens: unchanged

    def test_visitors_get_no_phone_menu(self):
        # Sign-in is a public page (templates/base_public.html): no app header, so no menu or theme toggle.
        self.client.logout()
        content = self.page(reverse("user_login"))
        self.assertNotIn('id="theme-toggle-menu"', content)
        self.assertNotIn('id="navbar-links"', content)

    def test_the_chip_names_the_app_as_its_label_and_tooltip(self):
        for url, name in ((reverse("albright_crypto_app:inbox"), "Opportunities"),
                          (reverse("albright_control:overview"), "Admin")):
            with self.subTest(url=url):
                content = self.page(url)
                self.assertIn(f'class="navbar__brand" aria-label="Albright - {name}"', content)
                self.assertIn(f'<span class="navbar__chip" title="{name}">', content)
                self.assertIn(f'<span class="navbar__chip-name">{name}</span></span></a>', content)

    def test_phone_css_one_row_icon_chip_and_tools_moved(self):
        css = self.phone_css()
        self.assertIn("@media (max-width: 639.98px) {", css)
        for rule in ("flex-wrap: nowrap; white-space: nowrap;", "min-height: 56px;",
                     "padding-top: env(safe-area-inset-top, 0px);", ".navbar__logo { height: 28px; }",
                     ".navbar__chip { justify-content: center; gap: 0; width: 34px; height: 28px; padding: 0; }",
                     ".navbar__chip-name { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0);",
                     "order: 2; margin-left: auto;", ".navbar__toggle { order: 3; margin-left: 0; }",
                     ".navbar--collapsed .navbar__tools .theme-toggle,\n  .navbar--collapsed .navbar__account { display: none; }",
                     ".navbar--collapsed .navbar__menu-extra { display: block;"):
            self.assertIn(rule, css)
        self.assertIn("\n.navbar__menu-extra { display: none; }", STYLE.read_text(encoding="utf-8"))

    def test_scripts_never_stack_on_phones_and_both_theme_toggles_work(self):
        content = self.page(reverse("trading_home"))
        self.assertIn("if (collapse && !phone.matches && overflows()) nav.classList.add('navbar--stacked');", content)
        self.assertIn("['theme-toggle', 'theme-toggle-menu'].forEach(", content)
