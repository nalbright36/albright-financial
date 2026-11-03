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
from django.test.html import parse_html
from django.urls import reverse

from albright_notes.models import Note

from .testing import with_apps

PASSWORD = "pw-not-real-12345"


def static(path):
    return pathlib.Path(finders.find(path)).read_text(encoding="utf-8")


def back_button(page):
    match = re.search(r'<a href="([^"]*)" class="back-chip" id="nav-back" data-main-paths="([^"]*)" data-no-return>', page)
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
        self.assertNotIn('id="back-row"', login)

    def test_it_sits_in_its_own_row_under_the_header_and_shows_on_phones_and_home_screen_apps(self):
        page = self.page(reverse("albright_planner:day"))
        nav = page[page.index('<nav class="navbar">'):page.index("</nav>")]
        self.assertNotIn('id="nav-back"', nav)                              # not in the header bar
        row = page.index('<div class="back-row container" id="back-row" data-mode="back" hidden>')
        self.assertLess(page.index("</nav>"), row)
        self.assertLess(row, page.index('id="nav-back"'))
        self.assertLess(row, page.index('class="container pl"'))           # above the page's content
        css = static("style.css")
        self.assertIn(".back-row { display: none;", css)                    # takes no space until something shows
        self.assertIn('@media (max-width: 640px), (display-mode: standalone) { .back-row[data-mode="back"]:not([hidden]) '
                      '{ display: flex; } }', css)                            # plain Back: phones and home screen apps
        self.assertIn('html.is-standalone .back-row[data-mode="back"]:not([hidden])', css)
        self.assertIn('.back-row[data-mode="return"]:not([hidden]) { display: flex; }', css)   # Back to <app>: anywhere
        self.assertIn("window.navigator.standalone", page)                  # iOS's home screen flag -> is-standalone
        js = static("js/app_nav.js")
        self.assertIn("window.history.back()", js)
        self.assertIn("backHidden = mains.indexOf(window.location.pathname) >= 0 && !hasPrevious", js)
        self.assertIn("row.hidden = backHidden;", js)


class OneBackControlTests(Base):
    """One back control on every page: a chip with the accent arrow - "← Back to <app>" (with its x) after coming
    from another app, else "← Back". The old plain button and its CSS are gone."""

    CHIP = re.compile(r'<div class="back-row container" id="back-row" data-mode="back" hidden>\s*'
                      r'<a href="(/[^"]*)" class="back-chip" id="nav-back" data-main-paths="[^"]*" data-no-return>'
                      r'<span class="back-chip__arrow" aria-hidden="true">&larr;</span> <span id="nav-back-label">Back</span></a>\s*'
                      r'<button type="button" class="back-chip__close" id="nav-back-close" aria-label="Dismiss" hidden>&times;</button>\s*</div>')

    def test_every_app_renders_exactly_one_back_control_in_chip_style(self):
        note = Note.objects.create(owner=self.me, title="Groceries", body="")
        for path in (reverse("albright_planner:day"), reverse("albright_planner:week"), reverse("albright_planner:month"),
                     reverse("albright_planner:new"), "/notes/", f"/notes/{note.pk}/", reverse("albright_meals:week"),
                     reverse("labs:hub"), reverse("albright_reselling_app:dashboard"), reverse("albright_crypto_app:inbox")):
            page = self.page(path)
            self.assertEqual(len(self.CHIP.findall(page)), 1, path)
            self.assertEqual(page.count('id="nav-back"'), 1, path)
            self.assertEqual(page.count("back-chip__arrow"), 1, path)
            for gone in ("return-chip", "back-row__back", "navbar__back"):
                self.assertNotIn(gone, page, path)
            parse_html(page)                                                  # balanced: no stray tags around it

    def test_the_old_plain_button_and_its_css_are_gone(self):
        css, js = static("style.css"), static("js/app_nav.js")
        for gone in (".back-row__back", ".return-chip", "navbar__back"):
            self.assertNotIn(gone, css)
        for gone in ("return-chip", "back-row__back"):
            self.assertNotIn(gone, js)
        self.assertIn(".back-chip__arrow { color: var(--accent); }", css)   # the accent arrow
        self.assertIn("border-radius: 999px;", css[css.index(".back-chip {"):css.index(".back-chip__arrow")])   # a pill

    def test_coming_from_another_app_it_says_back_to_that_app_otherwise_back(self):
        js = static("js/app_nav.js")
        show = js[js.index("function showBack(chip) {"):js.index("if (backLink) {")]
        # From another app: its exact page (same-site only), "Back to <app>", the x.
        self.assertIn('row.setAttribute("data-mode", "return");', show)
        self.assertIn('backLink.setAttribute("href", chip.url);', show)
        self.assertIn('backLabel.textContent = "Back to " + chip.label;', show)
        self.assertIn("if (backClose) backClose.hidden = !chip;", show)
        # Otherwise: "Back" - history back on this site, else the app's main page (the link's own href).
        self.assertIn('row.setAttribute("data-mode", "back");', show)
        self.assertIn('backLink.setAttribute("href", mainPage);', show)
        self.assertIn('backLabel.textContent = "Back";', show)
        self.assertIn('if (chip && (chip.key !== KEY || !sameSite(chip.url))) { chip = null;', js)
        # The x: the plain "Back" again.
        self.assertIn("showBack(null);                                             // plain \"← Back\" again", js)

    def test_the_fallback_is_the_apps_own_main_page(self):
        cases = {reverse("albright_planner:day"): "/planner/", "/notes/": "/notes/",
                 reverse("albright_meals:groceries"): reverse("albright_meals:week")}
        for path, home in cases.items():
            self.assertEqual(self.CHIP.search(self.page(path)).group(1), home, path)


class ReturnChipTests(Base):
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


class DateInputTests(Base):
    def test_date_and_time_inputs_fit_their_container_everywhere(self):
        css = static("style.css")
        rule = css[css.index('input[type="date"],\ninput[type="time"],\ninput[type="datetime-local"] {'):]
        rule = rule[:rule.index("}")]
        for part in ("width: 100%", "max-width: 100%", "min-width: 0", "box-sizing: border-box",
                     "-webkit-appearance: none", "text-align: left"):
            self.assertIn(part, rule)
        self.assertIn(':where(input[type="date"], input[type="time"], input[type="datetime-local"]) {', css)
        self.assertIn("::-webkit-date-and-time-value { text-align: left;", css)
        self.assertIn('.scanner-controls input[type="date"] { flex: 0 1 180px; width: auto; }', css)
