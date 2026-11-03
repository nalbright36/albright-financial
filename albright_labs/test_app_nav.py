"""Getting around without browser buttons (the iPhone home screen app): the header's Back button (its fallback is
the app's main page; it hides on a main page with no history), the "Back to <app>" return chip, the launcher's
Recent row and tiles that resume each app's last page (js/app_nav.js - same-site URLs only, kept per user), and
the header collapsing into the hamburger the same way on every page - Planner Day, Week and Month included."""
import pathlib
import re
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse

from albright_notes.models import Note

from .testing import with_apps

PASSWORD = "pw-not-real-12345"


def static(path):
    return pathlib.Path(finders.find(path)).read_text(encoding="utf-8")


def back_button(page):
    match = re.search(r'<a href="([^"]*)" class="navbar__back" id="nav-back" data-main-paths="([^"]*)" data-no-return hidden>', page)
    return match and {"href": match.group(1), "mains": match.group(2).split()}


class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD))
        self.client.force_login(self.me)

    def page(self, path):
        response = self.client.get(path)
        self.assertEqual(response.status_code, 200, path)
        return response.content.decode()


class BackButtonTests(Base):
    def test_planner_pages_fall_back_to_the_planner(self):
        mains = ["/planner/", "/planner/day/", "/planner/week/", "/planner/month/"]
        for name in ("day", "week", "month", "new"):
            back = back_button(self.page(reverse(f"albright_planner:{name}")))
            self.assertEqual(back["href"], "/planner/", name)
            self.assertEqual(back["mains"], mains)

    def test_each_app_falls_back_to_its_own_main_page(self):
        note = Note.objects.create(owner=self.me, title="Groceries", body="- milk")
        cases = [(f"/notes/{note.pk}/", "/notes/"), (reverse("albright_meals:groceries"), reverse("albright_meals:week")),
                 (reverse("labs:hub"), "/")]
        for path, home in cases:
            self.assertEqual(back_button(self.page(path))["href"], home, path)

    def test_only_same_site_and_only_signed_in(self):
        back = back_button(self.page(reverse("albright_planner:week")))
        self.assertTrue(back["href"].startswith("/") and not back["href"].startswith("//"))
        login = Client().get(reverse("user_login")).content.decode()
        self.assertNotIn('id="nav-back"', login)
        self.assertNotIn("data-nav-user", login)
        self.assertNotIn('id="return-chip-row"', login)

    def test_it_sits_left_of_the_logo_and_shows_on_phones_and_home_screen_apps(self):
        page = self.page(reverse("albright_planner:day"))
        self.assertLess(page.index('id="nav-back"'), page.index('class="navbar__brand"'))
        css = static("style.css")
        self.assertIn("@media (max-width: 640px), (display-mode: standalone) { .navbar__back:not([hidden]) "
                      "{ display: inline-flex; } }", css)
        self.assertIn("html.is-standalone .navbar__back:not([hidden])", css)
        self.assertIn("window.navigator.standalone", page)                  # iOS's home screen flag -> is-standalone
        js = static("js/app_nav.js")
        self.assertIn("window.history.back()", js)
        self.assertIn('back.hidden = mains.indexOf(window.location.pathname) >= 0 && !hasPrevious', js)


class ReturnChipTests(Base):
    def test_the_chip_is_on_every_signed_in_page_ready_to_fill(self):
        for path in (reverse("albright_planner:day"), "/notes/", reverse("albright_meals:week")):
            page = self.page(path)
            self.assertRegex(page, r'<div class="return-chip-row container" id="return-chip-row" hidden>\s*'
                                   r'<a href="/[^"]*" class="return-chip" id="return-chip" data-no-return>')
            self.assertIn('id="return-chip-label"', page)

    def test_body_tells_the_script_which_app_and_user(self):
        page = self.page(reverse("albright_planner:day"))
        self.assertIn(f'data-app-key="planner" data-app-label="Planner" data-nav-user="{self.me.pk}"', page)
        self.assertIn('data-app-key="notes" data-app-label="Notes"', self.page("/notes/"))
        self.assertIn('data-app-key="home" data-app-label="Home"', self.page(reverse("labs:hub")))

    def test_only_same_site_urls_are_kept_or_followed(self):
        js = static("js/app_nav.js")
        same_site = re.search(r"function sameSite\(url\) \{(.*?)\n    \}", js, re.S).group(1)
        for guard in ('url.charAt(0) !== "/"', 'url.charAt(1) === "/"', 'url.charAt(1) === "\\\\"',
                      ".origin === window.location.origin"):
            self.assertIn(guard, same_site)
        self.assertIn("link.origin !== window.location.origin", js)        # only same-site links are remembered
        self.assertIn("from.key !== KEY && sameSite(from.url)", js)        # only from another app, only same-site
        self.assertIn("!sameSite(chip.url)", js)
        self.assertIn('sessionStorage", "nav_from"', js)

    def test_switching_apps_on_purpose_shows_no_chip(self):
        page = self.page(reverse("albright_planner:day"))
        self.assertIn('class="navbar__brand">', page)
        self.assertIn('link.closest("[data-no-return], .navbar__brand")', static("js/app_nav.js"))   # the logo
        tiles = re.findall(r'<a [^>]*class="app-launcher__tile[^"]*"[^>]*>', page)
        self.assertTrue(tiles)
        self.assertTrue(all("data-no-return" in tile for tile in tiles))
        hub = self.page(reverse("labs:hub"))
        picks = re.findall(r'<a [^>]*data-pick-link[^>]*>', hub)
        self.assertTrue(picks)
        self.assertTrue(all("data-no-return" in pick for pick in picks))


class ResumeAndRecentTests(Base):
    def test_the_launcher_has_a_recent_row_above_the_tiles(self):
        page = self.page(reverse("albright_planner:day"))
        panel = page.split('id="app-launcher-panel"', 1)[1]
        self.assertLess(panel.index('id="app-launcher-recent"'), panel.index('class="app-launcher__grid"'))
        self.assertIn('<div class="app-launcher__recent" id="app-launcher-recent" hidden>', panel)
        self.assertIn('id="app-launcher-recent-list"', panel)
        self.assertLess(page.index("labs/launcher.js"), page.index("js/app_nav.js"))
        self.assertLess(page.index("js/app_nav.js"), page.index("albright_planner/planner.js"))

    def test_tiles_resume_the_last_page_and_recent_shows_four(self):
        js = static("js/app_nav.js")
        self.assertIn('".app-launcher__tile[data-app-key]"', js)          # the launcher's tiles
        self.assertIn('".labs-pick[data-key] [data-pick-link]"', js)      # Home's tiles
        self.assertIn('key !== KEY && key !== "home" && sameSite(url)', js)
        self.assertIn("RECENT_SHOW = 4", js)
        self.assertIn('LAST = "nav_last:" + user, RECENT = "nav_recent:" + user', js)   # kept per user
        hub = self.page(reverse("labs:hub"))
        self.assertRegex(hub, r'<li class="labs-pick[^"]*" data-key="planner"')

    def test_nothing_blocks_the_ios_edge_swipe(self):
        for path in ("js/app_nav.js", "labs/launcher.js", "albright_planner/planner.js", "albright_notes/notes.js",
                     "js/md_editor.js"):
            source = static(path)
            for blocker in ("touchstart", "touchmove", "touchend"):
                self.assertNotIn(blocker, source, path)
        page = self.page(reverse("albright_planner:day"))
        self.assertNotIn("touchstart", page)
        self.assertNotIn("touch-action: none", static("style.css"))
        self.assertNotIn("overscroll-behavior-x", static("style.css"))


class HamburgerTests(Base):
    """The same collapse rule on every page: the hamburger version of the header, checked as soon as the header
    exists, then on load, resize, rotation, back/forward, fonts, any size change of the header, and "navfit"."""

    def test_planner_day_week_and_month_render_the_hamburger_header(self):
        for name in ("day", "week", "month"):
            page = self.page(reverse(f"albright_planner:{name}"))
            self.assertIn('<button type="button" class="navbar__toggle" id="nav-toggle"', page, name)
            self.assertIn('<span class="navbar__toggle-bars" aria-hidden="true"><span></span><span></span><span></span></span>',
                          page)
            self.assertIn('<div class="navbar__links" id="navbar-links">', page)
            nav_end = page.index("</nav>")
            fit = page.index("var collapse = phone.matches || overflows();")
            self.assertLess(nav_end, fit)                                   # right after the header...
            self.assertLess(fit, page.index('class="container pl"'))       # ...before the page's content
            for trigger in ("'resize', refit", "'orientationchange', refit", "'load', refit", "'pageshow', refit",
                            "'navfit', refit", "new ResizeObserver(refit)", "document.fonts.ready.then(refit)"):
                self.assertIn(trigger, page, trigger)
            self.assertIn("window.matchMedia('(max-width: 640px)')", page)
            self.assertEqual(page.count('class="navbar"'), 1)

    def test_every_app_uses_the_same_header_check(self):
        note = Note.objects.create(owner=self.me, title="Groceries", body="")
        for path in (reverse("labs:hub"), f"/notes/{note.pk}/", reverse("albright_meals:week"),
                     reverse("albright_planner:new")):
            page = self.page(path)
            self.assertEqual(page.count("var collapse = phone.matches || overflows();"), 1, path)
            self.assertIn('id="nav-toggle"', page)

    def test_the_tabs_no_longer_open_a_page_inside_the_view_panel(self):
        # The cause of Day "expanding": the tabs were data-view links, so planner.js loaded the whole Day page -
        # header and all, never collapsed - into the view panel. Now: tabs are data-tab, and only a real panel opens.
        planner_js = static("albright_planner/planner.js")
        self.assertIn("event.target.closest('a[data-view=\"\"]')", planner_js)
        self.assertIn("if (html.indexOf('id=\"pl-view-panel\"') < 0) throw", planner_js)
        page = self.page(reverse("albright_planner:week"))
        self.assertNotRegex(page, r'class="pl-tab[^"]*" data-view=')
