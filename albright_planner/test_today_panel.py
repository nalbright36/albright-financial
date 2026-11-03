"""Home's Today panel and its "Show completed tasks" preference (UserPreference.show_completed_today, a sub-option
of "Show Today panel"): off (the default) - only open tasks (today's and overdue ones) and the events; on -
today's completed tasks too, struck through, at the bottom. The count line says what's shown. Checking a task off
in the panel takes it out at once with "Done · Undo" (planner.js)."""
import json
import pathlib
import re
from datetime import timedelta

from django.contrib.staticfiles import finders
from django.urls import reverse

from albright_labs.models import UserPreference

from . import planner
from .models import ChecklistItemCompletion, ItemCompletion, PlannerSettings, ScheduleItem, TaskChecklistItem
from .tests import TODAY, Base, url


def static(path):
    return pathlib.Path(finders.find(path)).read_text(encoding="utf-8")


class Panel(Base):
    def setUp(self):
        super().setUp()
        self.open = self.item("Water plants")
        self.done = self.item("Take out trash")
        ItemCompletion.objects.create(item=self.done, occurrence_date=TODAY)
        self.late = self.item("Late task", date=TODAY - timedelta(days=2))
        self.event = self.item("Standup", kind=ScheduleItem.EVENT, checkable=False)
        # Every subtask ticked, the box itself still open (its owner turned auto-complete off): finished too.
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"complete_when_checklist_done": False})
        self.listed = self.item("Pack bags")
        for n, text in enumerate(("Tent", "Stove")):
            row = TaskChecklistItem.objects.create(task=self.listed, order=n, text=text)
            ChecklistItemCompletion.objects.create(task=self.listed, checklist_item=row, occurrence_date=TODAY)

    def prefs(self, **fields):
        pref = UserPreference.for_user(self.me)
        for k, v in {"show_today_panel": True, **fields}.items():
            setattr(pref, k, v)
        pref.save()

    def panel(self):
        page = self.client.get(reverse("labs:hub")).content.decode()
        return page.split('id="planner-today"', 1)[1].split("</section>", 1)[0]

    @staticmethod
    def tasks(panel):
        column = panel.split(">Tasks<", 1)[1].split(">Events<", 1)[0]
        return re.findall(r'<span class="pl-item__title">([^<]+)', column)


class DefaultHidesCompletedTests(Panel):
    def test_off_by_default(self):
        self.assertFalse(UserPreference.for_user(self.me).show_completed_today)
        self.assertFalse(UserPreference._meta.get_field("show_completed_today").default)

    def test_only_open_tasks_overdue_first_and_the_events(self):
        self.prefs()
        panel = self.panel()
        self.assertEqual(self.tasks(panel), ["Late task", "Water plants"])     # no trash, no fully ticked list
        self.assertIn("Standup", panel.split(">Events<", 1)[1])
        self.assertIn('data-show-completed="0"', panel)

    def test_the_count_line_says_what_is_shown(self):
        self.prefs()
        self.assertIn('id="planner-today-count" data-open="2" data-overdue="1" data-done="0">2 open today &middot; 1 overdue</p>',
                      self.panel())

    def test_overdue_still_appears(self):
        self.prefs()
        panel = self.panel()
        li = panel.split(f'data-item="{self.late.pk}"', 1)[0].rsplit("<li", 1)[1]
        self.assertIn("is-overdue", li)
        self.assertIn(f'name="date" value="{(TODAY - timedelta(days=2)).isoformat()}"', panel)   # checks off that day

    def test_nothing_open(self):
        for item in (self.open, self.late):
            ItemCompletion.objects.create(item=item, occurrence_date=item.date)
        self.prefs()
        panel = self.panel()
        self.assertIn(">Nothing open today</p>", panel)
        self.assertIn("No tasks today.", panel)


class ShowCompletedTests(Panel):
    def test_completed_tasks_struck_through_at_the_bottom(self):
        self.prefs(show_completed_today=True)
        panel = self.panel()
        shown = self.tasks(panel)
        self.assertEqual(shown[:2], ["Late task", "Water plants"])                 # open first...
        self.assertEqual(sorted(shown[2:]), ["Pack bags", "Take out trash"])      # ...then the completed ones
        for item in (self.done, self.listed):
            li = panel.split(f'data-item="{item.pk}"', 1)[0].rsplit("<li", 1)[1]
            self.assertIn("is-done", li)                                          # struck through (planner.css)
        self.assertIn('data-show-completed="1"', panel)
        self.assertIn(">2 open today &middot; 1 overdue &middot; 2 done</p>", panel)

    def test_the_helper(self):
        shown = planner.today_panel(self.me, show_completed=True)
        self.assertEqual((shown["open"], shown["overdue"], shown["done"]), (2, 1, 2))
        self.assertEqual(planner.today_panel(self.me)["done"], 0)


class SettingTests(Panel):
    def test_saved_from_home_edit_and_my_account(self):
        self.prefs()
        apps = ["trade", "resell", "opps", "meals", "notes", "planner"]
        self.client.post(reverse("labs:save_picker"), {"order": apps, "visible": apps, "open_after": "home",
                                                       "today_panel": "1", "show_today": "1", "show_completed": "1"})
        self.assertTrue(UserPreference.for_user(self.me).show_completed_today)
        self.client.post(reverse("labs:save_landing"), {"open_after": "home", "today_panel": "1", "show_today": "1"})
        self.assertFalse(UserPreference.for_user(self.me).show_completed_today)   # unticked on My account

    def test_directly_under_show_today_panel_and_only_while_it_is_on(self):
        self.prefs(show_today_panel=False)
        page = self.client.get(reverse("labs:hub") + "?edit=1").content.decode()
        # Directly under it: the next thing after the "Show Today panel" label (a comment aside).
        self.assertRegex(page, r'id="show-today"[^>]*> Show Today panel <span[^>]*>[^<]*</span></label>\s*'
                               r'(\{#[^#]*#\}\s*)?<label class="labs-choice labs-choice--sub" id="show-completed-row"')
        self.assertRegex(page, r'<label class="labs-choice labs-choice--sub" id="show-completed-row" data-today-sub hidden>'
                               r'<input type="checkbox" name="show_completed" value="1" id="show-completed"> Show completed tasks')
        self.prefs(show_today_panel=True, show_completed_today=True)
        page = self.client.get(reverse("labs:hub") + "?edit=1").content.decode()
        self.assertIn('id="show-completed-row" data-today-sub><input type="checkbox" name="show_completed" value="1" '
                      'id="show-completed" checked>', page)
        self.assertIn("todaySub.hidden = !showToday.checked;", static("labs/hub.js"))
        self.assertIn(".labs-choice[hidden] { display: none; }", static("style.css"))


class CheckOffTests(Panel):
    def test_checking_off_and_undo_on_the_server(self):
        self.prefs()
        post = lambda: self.client.post(url("toggle", self.open.pk), {"date": TODAY.isoformat()},   # noqa: E731
                                        HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(json.loads(post().content)["done"], True)
        self.assertNotIn("Water plants", self.tasks(self.panel()))                # gone from the panel
        self.assertEqual(json.loads(post().content)["done"], False)               # Undo: the same box again
        self.assertIn("Water plants", self.tasks(self.panel()))

    def test_overdue_checked_off_leaves_the_panel(self):
        self.prefs()
        self.client.post(url("toggle", self.late.pk), {"date": self.late.date.isoformat()},
                         HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(self.tasks(self.panel()), ["Water plants"])

    def test_the_panel_has_done_undo(self):
        self.prefs()
        panel = self.panel()
        self.assertIn('<p class="pl-today__toast pl-small" id="planner-today-toast" role="status" aria-live="polite" hidden>'
                      '<span id="planner-today-toast-text">Done</span> &middot; '
                      '<button type="button" class="pl-today__undo" id="planner-today-undo">Undo</button></p>', panel)

    def test_the_script_removes_it_at_once_and_undo_puts_it_back(self):
        js = static("albright_planner/planner.js")
        self.assertIn("if (todayBox && todayBox.contains(el)) { todayChecked(el, res.data.done); return; }", js)
        checked = js[js.index("function todayChecked(el, done) {"):js.index("if (todayBox) {")]
        self.assertIn("list.removeChild(el);", checked)                          # hidden mode: out at once
        self.assertIn('todayToast("Done", true);', checked)
        self.assertIn("if (keep) list.appendChild(el);", checked)                # shown mode: to the bottom
        self.assertIn("todayCount({open: -1, overdue: overdue ? -1 : 0, done: keep ? 1 : 0});", checked)
        undo = js[js.index('getElementById("planner-today-undo").addEventListener'):js.index("// ---------- Checklist rows")]
        self.assertIn("d.list.insertBefore(d.el,", undo)                          # back where it was
        self.assertIn("post(form.action, new FormData(form))", undo)              # unchecked on the server
        self.assertIn("todayCount({open: 1, overdue: d.overdue ? 1 : 0});", undo)
        self.assertIn('(open ? open + " open today" : "Nothing open today")', js)    # the count line, as the server says it
