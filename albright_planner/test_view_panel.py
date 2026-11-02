"""The item view panel: clicking an item (Day, Week, Month, Home's Today panel) opens a read-only view of THAT
occurrence - a dialog over the calendar (?panel=1), and its own page for direct links. Checking off and checklist
ticks there save for that occurrence only; empty sections are left out; Edit (and "E") open the form, and saving
comes back to the panel; view-only shared items show no Edit and can't be checked off."""
import re
from urllib.parse import parse_qs, urlsplit

from django.urls import reverse

from albright_labs.models import UserPreference
from albright_notes.models import Note

from . import checklist
from .models import ItemCompletion, ItemShare, ScheduleItem
from .templatetags.planner_extras import notes_html
from .tests import TODAY, Base, d, url


def view(item, day=None, **extra):
    query = {"date": (day or item.date).isoformat(), **extra}
    return url("item", item.pk) + "?" + "&".join(f"{k}={v}" for k, v in query.items())


class Fixtures(Base):
    def panel(self, item, day=None, client=None, **extra):
        response = (client or self.client).get(view(item, day, panel="1", **extra))
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def links(self, page):
        """The view-panel links on a page: [(item id, date)]."""
        return re.findall(r'href="/planner/item/(\d+)/\?date=([\d-]+)[^"]*"[^>]*data-view', page)


class OpeningTests(Fixtures):
    def test_day_week_month_and_home_link_to_the_view_not_the_form(self):
        item = self.item("Water plants", recurrence="FREQ=DAILY", recurrence_count=10)
        for name in ("day", "week", "month"):
            with self.subTest(view=name):
                page = self.client.get(url(name)).content.decode()
                self.assertIn((str(item.pk), TODAY.isoformat()), self.links(page))
                self.assertNotIn(f'href="{url("edit", item.pk)}?date=', page)
                self.assertIn('data-view-url="/planner/item/0/"', page)
        pref = UserPreference.for_user(self.me)
        pref.show_today_panel = True
        pref.save()
        home = self.client.get("/?picker=1").content.decode()
        self.assertIn('id="planner-today"', home)
        self.assertIn((str(item.pk), TODAY.isoformat()), self.links(home))

    def test_each_occurrence_links_to_its_own_date(self):
        item = self.item("Standup", recurrence="FREQ=DAILY", recurrence_count=10)
        week = self.client.get(url("week")).content.decode()
        days = {day for pk, day in self.links(week) if pk == str(item.pk)}
        self.assertEqual(days, {f"2026-11-0{n}" for n in range(4, 9)})       # Wed Nov 4 .. Sun Nov 8

    def test_the_panel_is_the_view_for_that_occurrence(self):
        item = self.item("Standup", recurrence="FREQ=DAILY", recurrence_count=10, start_time="09:30")
        ItemCompletion.objects.create(item=item, occurrence_date=d(11, 5), completed_by=self.me)
        panel = self.panel(item, d(11, 5))
        self.assertNotIn("<html", panel)                                  # just the panel, for the dialog
        self.assertNotIn('id="pl-form"', panel)                           # not the edit form
        self.assertIn('data-date="2026-11-05"', panel)
        self.assertIn("Thu, Nov 5", panel)
        self.assertIn("9:30 AM", panel)
        self.assertRegex(panel, r'id="pl-view-check"[^>]*aria-checked="true"')
        self.assertRegex(self.panel(item, d(11, 6)), r'id="pl-view-check"[^>]*aria-checked="false"')

    def test_direct_url_is_a_full_page(self):
        item = self.item("Dentist", kind="event", checkable=False, notes="Bring insurance card")
        page = self.client.get(view(item, next="/planner/week/")).content.decode()
        self.assertIn("<html", page)
        self.assertIn('class="pl-view-page"', page)
        self.assertIn("Dentist", page)
        self.assertIn("All day", page)
        self.assertIn('href="/planner/week/" class="pl-btn" data-view-close', page)     # Close goes back
        self.assertNotIn('id="pl-view-check"', page)                                   # events: no checkbox

    def test_a_bad_or_missing_date_falls_back_to_the_current_occurrence(self):
        item = self.item("Standup", recurrence="FREQ=DAILY", recurrence_count=10)
        for day in ("", "not-a-date", "2026-12-25"):
            with self.subTest(date=day):
                page = self.client.get(url("item", item.pk) + f"?date={day}&panel=1").content.decode()
                self.assertIn(f'data-date="{TODAY.isoformat()}"', page)

    def test_strangers_get_404(self):
        item = self.item("Private")
        self.assertEqual(self.as_user(self.stranger).get(view(item)).status_code, 404)

    def test_a_moved_occurrence_shows_its_new_day(self):
        item = self.item("Standup", recurrence="FREQ=DAILY", recurrence_count=10)
        self.client.post(url("move", item.pk), {"date": "2026-11-05", "to": "2026-11-09", "next": "/planner/day/"})
        panel = self.panel(item, d(11, 5))
        self.assertIn("Mon, Nov 9", panel)
        self.assertIn('data-date="2026-11-05"', panel)                    # still keyed by its scheduled day


class LayoutTests(Fixtures):
    def test_details_line(self):
        item = self.item("Gym", recurrence="FREQ=WEEKLY;BYDAY=MO", date=d(11, 2), recurrence_until=d(12, 31),
                         priority="high", start_time="18:00", end_time="19:00")
        panel = self.panel(item, d(11, 9))
        self.assertIn("Mon, Nov 9", panel)
        self.assertIn("6:00 PM&ndash;7:00 PM", panel)
        self.assertIn("Every Monday until Dec 31", panel)
        self.assertRegex(panel, r'class="pl-badge pl-badge--high" id="pl-view-priority">High<')
        several = self.item("Run", recurrence="FREQ=WEEKLY;BYDAY=MO,WE,FR", recurrence_count=9)
        self.assertIn("Every Monday, Wednesday and Friday, 9 times", self.panel(several))

    def test_empty_sections_are_hidden(self):
        item = self.item("Plain")
        panel = self.panel(item)
        for section in ('id="pl-view-checklist"', 'id="pl-view-notes"', 'id="pl-view-note"', 'id="pl-view-shared"',
                        'id="pl-view-repeat"', 'id="pl-view-priority"', 'id="pl-view-skip"'):
            self.assertNotIn(section, panel)
        self.assertIn('id="pl-view-edit"', panel)
        self.assertIn('id="pl-view-duplicate"', panel)

    def test_full_panel(self):
        note = Note.objects.create(owner=self.me, title="Packing list", body="Bring the big tent\nand more")
        item = self.item("Camping", notes="- tent\n- stove\n\nLeave by 8", note=note)
        checklist.create_rows(item, ["Tent", "Stove", "Lantern"])
        checklist.set_checked(item.checklist.get(text="Tent"), TODAY, True, self.me)
        ItemShare.objects.create(item=item, user=self.wife, permission=ItemShare.EDIT)
        panel = self.panel(item)
        self.assertIn('id="pl-view-checklist"', panel)
        self.assertIn("1 of 3 done", panel)
        self.assertNotIn('name="cl_text"', panel)                         # no editing item text here
        self.assertIn("<ul><li>tent</li><li>stove</li></ul><p>Leave by 8</p>", panel)
        self.assertIn('data-clamp', panel)
        self.assertIn("Packing list", panel)
        self.assertIn("Bring the big tent", panel)
        self.assertIn(f'href="{reverse("albright_notes:note", args=[note.pk])}" class="pl-view__open-note">Open in Notes',
                      panel)
        self.assertRegex(panel, r'id="pl-view-shared">.*Shared with sam')

    def test_shared_by_shows_the_owner(self):
        item = self.item("Groceries", owner=self.wife)
        ItemShare.objects.create(item=item, user=self.me, permission=ItemShare.EDIT)
        panel = self.panel(item)
        self.assertRegex(panel, r'id="pl-view-shared"><span class="pl-avatar"[^>]*>S</span> Shared by sam')
        self.assertNotIn("view only", panel)

    def test_notes_render_escaped_with_line_breaks_and_lists(self):
        self.assertEqual(notes_html("Line one\nline two\n\n1. a\n2. <b>b</b>"),
                         "<p>Line one<br>line two</p><ol><li>a</li><li>&lt;b&gt;b&lt;/b&gt;</li></ol>")
        self.assertEqual(notes_html(""), "")

    def test_menu(self):
        series = self.item("Standup", recurrence="FREQ=DAILY", recurrence_count=10)
        panel = self.panel(series)
        for action in ("pl-view-skip", "pl-view-move", "pl-view-duplicate", "pl-view-delete"):
            self.assertIn(f'id="{action}"', panel)
        self.assertIn("Delete all occurrences", panel)
        self.assertNotIn('id="pl-view-skip"', self.panel(self.item("One-off")))


class ActionTests(Fixtures):
    def test_completing_from_the_view_saves_that_occurrence_only(self):
        item = self.item("Standup", recurrence="FREQ=DAILY", recurrence_count=10)
        panel = self.panel(item, d(11, 6))
        form = re.search(r'<form method="post" action="([^"]+)" class="pl-check-form">.*?name="date" value="([^"]+)"',
                         panel, re.S)
        self.assertEqual(form.group(1), url("toggle", item.pk))
        response = self.client.post(form.group(1), {"date": form.group(2)}, HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertTrue(response.json()["done"])
        self.assertEqual(list(item.completions.values_list("occurrence_date", flat=True)), [d(11, 6)])

    def test_checklist_ticks_from_the_view_save_that_occurrence_only(self):
        item = self.item("Pack", recurrence="FREQ=DAILY", recurrence_count=10)
        checklist.create_rows(item, ["Tent", "Stove"])
        panel = self.panel(item, d(11, 6))
        row = item.checklist.get(text="Tent")
        self.assertIn(f'action="{url("checklist_toggle", item.pk, row.pk)}"', panel)
        self.assertIn('name="date" value="2026-11-06"', panel)
        response = self.client.post(url("checklist_toggle", item.pk, row.pk), {"date": "2026-11-06"},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual((response.json()["progress"], response.json()["progress_title"]), ("1/2", "1 of 2 done"))
        self.assertEqual([r.checked for r in checklist.occurrence_rows(item, d(11, 6))], [True, False])
        self.assertEqual([r.checked for r in checklist.occurrence_rows(item, d(11, 7))], [False, False])
        self.assertIn("1 of 2 done", self.panel(item, d(11, 6)))
        self.assertIn("0 of 2 done", self.panel(item, d(11, 7)))

    def test_no_js_check_off_comes_back_to_the_page(self):
        item = self.item("Call mom")
        response = self.client.post(url("toggle", item.pk), {"date": TODAY.isoformat(),
                                                             "next": view(item, next="/planner/day/")})
        self.assertRedirects(response, view(item, next="/planner/day/"), fetch_redirect_response=False)

    def test_move_from_the_menu(self):
        item = self.item("Dentist")
        response = self.client.post(url("move", item.pk), {"date": TODAY.isoformat(), "to": "2026-11-10",
                                                           "next": "/planner/week/"})
        self.assertRedirects(response, "/planner/week/", fetch_redirect_response=False)
        item.refresh_from_db()
        self.assertEqual(item.date, d(11, 10))
        drag = self.client.post(url("move", item.pk), {"date": "2026-11-10", "to": "2026-11-11"},
                                content_type="application/json")
        self.assertEqual(drag.json(), {"ok": True})                       # Week's drag still gets JSON

    def test_duplicate(self):
        item = self.item("Gym", recurrence="FREQ=WEEKLY;BYDAY=MO", date=d(11, 2), recurrence_count=5, priority="high")
        checklist.create_rows(item, ["Shoes", "Water"])
        checklist.set_checked(item.checklist.get(text="Shoes"), d(11, 2), True, self.me)
        response = self.client.post(url("duplicate", item.pk), {"next": "/planner/week/"})
        self.assertRedirects(response, "/planner/week/", fetch_redirect_response=False)
        copy = ScheduleItem.objects.exclude(pk=item.pk).get()
        self.assertEqual((copy.title, copy.recurrence, copy.recurrence_count, copy.priority, copy.owner),
                         ("Gym", item.recurrence, 5, "high", self.me))
        self.assertEqual([r.text for r in copy.checklist.all()], ["Shoes", "Water"])
        self.assertEqual([r.checked for r in checklist.occurrence_rows(copy, d(11, 2))], [False, False])

    def test_view_only_can_duplicate_into_their_own_item(self):
        item = self.item("Dinner plans")
        ItemShare.objects.create(item=item, user=self.wife, permission=ItemShare.VIEW)
        self.as_user(self.wife).post(url("duplicate", item.pk))
        self.assertEqual(ScheduleItem.objects.get(owner=self.wife).title, "Dinner plans")


class EditTests(Fixtures):
    def test_edit_button_opens_the_form_for_that_occurrence(self):
        item = self.item("Standup", recurrence="FREQ=DAILY", recurrence_count=10)
        panel = self.panel(item, d(11, 6), next="/planner/week/")
        edit = re.search(r'<a href="([^"]+)" class="pl-btn pl-btn--primary" id="pl-view-edit" data-view-edit', panel)
        self.assertIsNotNone(edit)
        href = edit.group(1).replace("&amp;", "&")
        parts = urlsplit(href)
        self.assertEqual(parts.path, url("edit", item.pk))
        self.assertEqual(parse_qs(parts.query), {"date": ["2026-11-06"], "next": ["/planner/week/"], "then": ["view"]})
        form = self.client.get(href).content.decode()
        self.assertIn('id="pl-form"', form)
        self.assertIn('name="then" value="view"', form)
        back = view(item, d(11, 6), next="/planner/week/").replace("&", "&amp;")
        self.assertIn(f'href="{back}" class="pl-back"', form.replace("%2F", "/"))          # Back / Cancel return to the panel

    def test_e_shortcut_targets_the_edit_button(self):
        """planner.js: "E" (outside a text field) follows the panel's [data-view-edit] link."""
        js = open("albright_planner/static/albright_planner/planner.js", encoding="utf-8").read()
        self.assertIn('event.key !== "e" && event.key !== "E"', js)
        self.assertIn('panel.querySelector("[data-view-edit]")', js)
        self.assertIn('data-view-edit', self.panel(self.item("Anything")))

    def form(self, item, **changes):
        data = {"title": item.title, "kind": item.kind, "date": item.date.isoformat(), "priority": "none",
                "repeat": "none", "ends": "never", "checkable": "on", "next": "/planner/day/"}
        data.update(changes)
        return data

    def test_after_saving_the_view_shows_the_updated_item(self):
        item = self.item("Call mom")
        response = self.client.post(url("edit", item.pk), self.form(item, title="Call mom back", date="2026-11-07",
                                                                    then="view"))
        target = view(item, d(11, 7), next="/planner/day/").replace("/planner/day/", "%2Fplanner%2Fday%2F")
        self.assertRedirects(response, target, fetch_redirect_response=False)
        page = self.client.get(response["Location"]).content.decode()
        self.assertIn("Call mom back", page)
        self.assertIn("Sat, Nov 7", page)

    def test_from_the_dialog_saving_reopens_it_over_the_calendar(self):
        item = self.item("Call mom")
        response = self.client.post(url("edit", item.pk), self.form(item, title="Call dad", then="panel"))
        self.assertRedirects(response, f"/planner/day/#pl-view={item.pk}-{TODAY.isoformat()}",
                             fetch_redirect_response=False)
        self.assertIn("Call dad", self.panel(item))

    def test_saving_a_series_from_this_occurrence_on_views_the_new_series(self):
        item = self.item("Standup", recurrence="FREQ=DAILY", recurrence_count=10)
        data = self.form(item, title="Standup v2", date="2026-11-06", repeat="daily", ends="after", count="10",
                         scope="following", occurrence="2026-11-06", then="view")
        response = self.client.post(url("edit", item.pk), data)
        new = ScheduleItem.objects.get(title="Standup v2")
        self.assertIn(f"/planner/item/{new.pk}/?date=2026-11-06", response["Location"])

    def test_without_then_the_form_still_goes_to_next(self):
        item = self.item("Call mom")
        response = self.client.post(url("edit", item.pk), self.form(item, title="Call"))
        self.assertRedirects(response, "/planner/day/", fetch_redirect_response=False)


class PermissionTests(Fixtures):
    def test_view_only_shows_no_edit_and_cant_complete(self):
        item = self.item("Dinner plans", notes="Tacos")
        checklist.create_rows(item, ["Shells"])
        ItemShare.objects.create(item=item, user=self.wife, permission=ItemShare.VIEW)
        wife = self.as_user(self.wife)
        panel = self.panel(item, client=wife)
        self.assertNotIn('id="pl-view-edit"', panel)
        self.assertNotIn("data-view-edit", panel)
        self.assertNotIn('class="pl-check-form"', panel)
        self.assertRegex(panel, r'class="pl-check pl-check--lg is-readonly"')
        self.assertNotIn("data-checklist-toggle", panel)
        for action in ("pl-view-skip", "pl-view-move", "pl-view-delete"):
            self.assertNotIn(f'id="{action}"', panel)
        self.assertIn("view only", panel)
        self.assertEqual(wife.post(url("toggle", item.pk), {"date": TODAY.isoformat()}).status_code, 404)
        row = item.checklist.get()
        self.assertEqual(wife.post(url("checklist_toggle", item.pk, row.pk), {"date": TODAY.isoformat()}).status_code, 404)
        self.assertFalse(item.completions.exists())

    def test_edit_share_can_complete_and_edit_but_not_delete(self):
        item = self.item("Groceries")
        ItemShare.objects.create(item=item, user=self.wife, permission=ItemShare.EDIT)
        wife = self.as_user(self.wife)
        panel = self.panel(item, client=wife)
        self.assertIn('id="pl-view-edit"', panel)
        self.assertIn('class="pl-check-form"', panel)
        self.assertNotIn('id="pl-view-delete"', panel)
        wife.post(url("toggle", item.pk), {"date": TODAY.isoformat()})
        self.assertTrue(item.completions.filter(occurrence_date=TODAY, completed_by=self.wife).exists())
