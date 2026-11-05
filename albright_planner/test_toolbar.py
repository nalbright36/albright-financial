"""The Planner's toolbar: only the date range, "‹ Today ›", View options and New item. View options (a dialog) holds
the view, the filters, Show meals, the default sharing and a link to the settings page - Save stores them for the
account and they apply on the next load, anywhere; a ?kind= / ?done= / ?who= / ?meals= link (and the live preview)
overrides them without saving; its button's dot shows while something is filtered out. New item is a dialog with
quick add first and "More options" bringing in the full form (new/?panel=1). Keys: D / W / M, T, N."""
import pathlib
import re

from django.contrib.staticfiles import finders
from django.urls import reverse
from django.test import Client

from albright_labs.testing import share_with

from .models import ItemCompletion, PlannerSettings, ScheduleItem
from .tests import TODAY, Base, url
from .views import is_filtered


def static(path):
    return pathlib.Path(finders.find(path)).read_text(encoding="utf-8")


def bar(page):
    """The toolbar itself - the head, without its two dialogs."""
    head = page[page.index('<div class="pl-head" id="pl-head">'):]
    return head[:head.index("<dialog")]


def dialog(page, dialog_id):
    start = page.index(f'id="{dialog_id}"')
    return page[start:page.index("</dialog>", start)]


def titles(page):
    return re.findall(r'class="pl-item__title[^"]*"[^>]*>([^<]+)<', page)


class ToolbarTests(Base):
    def test_the_bar_has_only_the_kept_controls(self):
        for name in ("day", "week", "month"):
            head = bar(self.client.get(url(name)).content.decode())
            self.assertEqual(re.findall(r'<(?:a|button)\b[^>]*\bid="([^"]+)"', head),
                             ["pl-prev", "pl-today", "pl-next", "pl-options-btn", "pl-new"], name)
            self.assertIn('<h1 class="pl-title" id="pl-title">', head)
            for gone in ("<select", "<form", 'type="checkbox"', "pl-tab", "Quick add", "Settings</a>", "Show meals"):
                self.assertNotIn(gone, head, name)
            self.assertIn("View options", head)
            self.assertIn("<svg", head.split('id="pl-options-btn"', 1)[1].split("</a>", 1)[0])    # the sliders icon

    def test_week_title_is_the_date_range(self):
        page = self.client.get(url("week")).content.decode()
        self.assertIn('id="pl-title">Nov 2 – 8, 2026</h1>', page)

    def test_view_options_holds_everything_else(self):
        self.wife.first_name = "Sam"
        self.wife.save()
        options = dialog(self.client.get(url("day")).content.decode(), "pl-options")
        self.assertIn('action="/planner/view-options/"', options)
        self.assertEqual(re.findall(r'name="view" value="(\w+)"', options), ["day", "week", "month"])
        self.assertIn('name="view" value="day" data-url="/planner/day/?date=2026-11-04" checked', options)
        self.assertEqual(re.findall(r'<option value="(\w*)"[^>]*>(Tasks and events|Tasks only|Events only)</option>', options),
                         [("", "Tasks and events"), ("task", "Tasks only"), ("event", "Events only")])
        self.assertRegex(options, r'<span>Hide completed</span><input type="checkbox" name="hide_done" value="1" role="switch"')
        self.assertEqual(re.findall(r'name="who" value="(\w*)"[^>]*><span>([^<]+)</span>', options),
                         [("", "All"), ("mine", "Mine"), ("shared", "Shared with me")])
        self.assertRegex(options, r'Show meals</span><input type="checkbox" name="show_meals" value="1" role="switch"[^>]* checked>')
        self.assertIn("New items: Only me", options)                                  # the default lives in Circles
        self.assertNotIn('name="default_share"', options)
        self.assertIn('href="/planner/settings/" class="pl-opt-link"', options)
        self.assertIn(">Save</button>", options)
        self.assertIn("data-options-cancel>Cancel</button>", options)

    def test_show_meals_only_for_people_who_can_open_meals(self):
        from albright_labs.testing import with_apps
        with_apps(self.me, ["planner"])
        options = dialog(self.client.get(url("day")).content.decode(), "pl-options")
        self.assertNotIn("show_meals", options)

    def test_dialogs_are_solid_and_a_bottom_sheet_on_phones(self):
        css = static("albright_planner/planner.css")
        sheet = css[css.index(".pl-sheet { "):css.index(".pl-sheet--new {")]
        self.assertIn("background: var(--pl-sheet-bg)", sheet)
        self.assertIn("--pl-sheet-bg: var(--glass-bg-solid, var(--surface))", sheet)
        self.assertIn("backdrop-filter: none", sheet)
        phone = css[css.index(".pl-sheet, .pl-sheet--new { width: 100%"):]
        self.assertIn("margin: auto 0 0;", phone[:200])                            # pinned to the bottom
        self.assertIn("border-radius: 16px 16px 0 0;", phone[:200])
        self.assertIn("padding-bottom: calc(12px + env(safe-area-inset-bottom));", phone)

    def test_phone_layout_two_lines_that_never_wrap(self):
        css = static("albright_planner/planner.css")
        self.assertIn(".pl-head__tools { display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; }", css)
        phone = css[css.index("@media (max-width: 640px) {\n  .pl-head { flex-direction: column;"):]
        self.assertIn(".pl-head { flex-direction: column; align-items: stretch; gap: 10px; }", phone)   # date range alone
        self.assertIn(".pl-options-btn__text { position: absolute;", phone)        # View options: just its icon
        self.assertIn("@media (max-width: 380px) {", css)                          # 320px phones still fit


class ViewOptionsSaveTests(Base):
    def setUp(self):
        super().setUp()
        self.item("Open task")
        done = self.item("Finished task")
        ItemCompletion.objects.create(item=done, occurrence_date=TODAY)
        self.item("Team event", kind=ScheduleItem.EVENT)
        share_with(self.item("Sam task", owner=self.wife), self.me, "view")

    def save(self, **fields):
        data = {"view": "day", "date": TODAY.isoformat(), "kind": "", "who": "", **fields}
        return self.client.post(url("view_options"), data)

    def test_save_stores_them_for_the_account(self):
        response = self.save(view="week", kind="task", hide_done="1", who="mine")
        prefs = PlannerSettings.for_user(self.me)
        self.assertEqual((prefs.filter_kind, prefs.hide_done, prefs.filter_who, prefs.show_meals), ("task", True, "mine", False))
        self.assertRedirects(response, url("week"), fetch_redirect_response=False)   # the chosen view, today
        self.assertEqual(response.cookies["planner_view"].value, "week")          # /planner/ opens it next time

    def test_saved_choices_apply_on_reload_and_on_another_device(self):
        self.save(hide_done="1", kind="task", show_meals="1")
        for client in (self.client, self.as_user(self.me)):                        # this browser and "the phone"
            page = client.get(url("day")).content.decode()
            shown = titles(page.split('id="pl-day"', 1)[1])
            self.assertIn("Open task", shown)
            self.assertNotIn("Finished task", shown)                              # Hide completed
            self.assertNotIn("Team event", shown)                                 # Tasks only
            options = dialog(page, "pl-options")
            self.assertIn('id="pl-opt-done" checked', options)
            self.assertIn('<option value="task" selected>Tasks only</option>', options)
            self.assertNotIn('id="pl-options-dot" hidden', page)                  # the dot: something filtered out

    def test_a_link_or_the_live_preview_overrides_without_saving(self):
        self.save(hide_done="1", show_meals="1")
        preview = self.client.get(url("day") + "?done=1&kind=&who=&meals=1").content.decode()
        self.assertIn("Finished task", titles(preview.split('id="pl-day"', 1)[1]))
        self.assertTrue(PlannerSettings.for_user(self.me).hide_done)              # nothing saved
        self.assertIn("done=1", preview.split('id="pl-next"')[0].rsplit("href=", 1)[1])   # the override follows the arrows
        self.assertNotIn("done=", self.client.get(url("day")).content.decode().split('id="pl-next"')[0].rsplit("href=", 1)[1])

    def test_whose_items_saved(self):
        self.save(who="shared", show_meals="1")
        page = self.client.get(url("day")).content.decode().split('id="pl-day"', 1)[1]
        self.assertIn("Sam task", page)
        self.assertNotIn("Open task", titles(page))

    def test_bad_values_fall_back_to_the_defaults(self):
        self.save(kind="everything", who="others", view="year")
        prefs = PlannerSettings.for_user(self.me)
        self.assertEqual((prefs.filter_kind, prefs.filter_who), ("", ""))
        self.assertEqual(self.save(view="year")["Location"], url("day"))

    def test_view_options_link_to_the_sharing_default(self):
        from albright_labs import sharing
        self.wife.first_name = "Sam"
        self.wife.save()
        page = self.client.get(url("day")).content.decode()
        self.assertIn("New items: Only me", page)
        self.assertIn(reverse("labs:sharing_defaults") + "#default-planner", page)
        sharing.save_default(self.me, "planner", "people", people={str(self.wife.pk): "view"})
        self.save(view="day")                                                        # View options leave it alone
        self.assertEqual(sharing.default_text(self.me, "planner"), "Sam")

    def test_post_only_and_signed_in_only(self):
        self.assertEqual(self.client.get(url("view_options")).status_code, 405)
        self.assertEqual(Client().post(url("view_options"), {"hide_done": "1"}).status_code, 302)   # to sign in
        self.assertFalse(PlannerSettings.for_user(self.me).hide_done)


class FilteredDotTests(Base):
    DEFAULTS = {"kind": "", "show_done": True, "who": "", "meals": True}

    def test_defaults_show_no_dot(self):
        self.assertFalse(is_filtered(self.DEFAULTS, meals_toggle=True))
        self.assertIn('id="pl-options-dot" hidden', self.client.get(url("day")).content.decode())

    def test_any_filter_shows_the_dot(self):
        for change in ({"kind": "task"}, {"kind": "event"}, {"show_done": False}, {"who": "mine"}, {"who": "shared"},
                       {"meals": False}):
            self.assertTrue(is_filtered({**self.DEFAULTS, **change}, meals_toggle=True), change)

    def test_hidden_meals_count_only_for_people_who_can_open_meals(self):
        self.assertFalse(is_filtered({**self.DEFAULTS, "meals": False}, meals_toggle=False))

    def test_the_dot_on_the_page(self):
        page = self.client.get(url("day") + "?done=0").content.decode()
        self.assertIn('<span class="pl-options-dot" id="pl-options-dot"><span class="visually-hidden"> (some items are filtered out)</span></span>', page)
        PlannerSettings.for_user(self.me)
        PlannerSettings.objects.filter(user=self.me).update(show_meals=False)
        self.assertNotIn('id="pl-options-dot" hidden', self.client.get(url("week")).content.decode())

    def test_view_and_sharing_are_not_filters(self):
        from albright_labs import sharing
        sharing.save_default(self.me, "planner", "people", people={str(self.wife.pk): "edit"})
        self.assertIn('id="pl-options-dot" hidden', self.client.get(url("month")).content.decode())

    def test_the_script_uses_the_same_rule_live(self):
        js = static("albright_planner/planner.js")
        rule = js[js.index("function filtered() {"):js.index("function previewUrl()")]
        for part in ("optionsForm.elements.kind.value", "optionsForm.elements.hide_done.checked", 'chosen("who")',
                     "(meals && !meals.checked)"):
            self.assertIn(part, rule)
        self.assertIn("dot.hidden = !filtered();", js)


class LiveAndCancelTests(Base):
    def test_changes_apply_live_and_cancel_puts_the_page_back(self):
        js = static("albright_planner/planner.js")
        self.assertIn('optionsForm.addEventListener("change", function (event) {', js)
        self.assertIn("fetch(previewUrl(), {credentials: \"same-origin\"})", js)      # the page underneath, not saved
        for param in ('"kind"', '"done"', '"who"', '"meals"'):
            self.assertIn(f"url.searchParams.set({param}", js)
        cancel = js[js.index("function cancelOptions() {"):js.index("if (options && optionsForm && options.showModal)")]
        self.assertIn("putContent(saved.nodes); showHead(saved.links);", cancel)
        self.assertIn("optionsForm.reset();", cancel)
        self.assertIn('options.addEventListener("cancel"', js)                     # Esc is Cancel too
        self.assertIn('window.dispatchEvent(new Event("navfit"));', js)           # the header re-checks its fit


class NewItemDialogTests(Base):
    def test_quick_add_first_then_more_options(self):
        page = self.client.get(url("day")).content.decode()
        new = dialog(page, "pl-new-dialog")
        self.assertLess(new.index('id="pl-quick"'), new.index('id="pl-new-more"'))
        self.assertIn('action="/planner/add/"', new)
        self.assertIn('placeholder="Gym Mon Wed Fri 6am"', new)
        self.assertRegex(new, r'<a href="/planner/new/\?date=2026-11-04&amp;next=%2Fplanner%2Fday%2F" class="pl-new-more" '
                              r'id="pl-new-more" aria-expanded="false" aria-controls="pl-new-full">More options</a>')
        self.assertIn('<div class="pl-new-full" id="pl-new-full" hidden></div>', new)
        self.assertIn('href="/planner/new/?date=2026-11-04', bar(page).split('id="pl-new"', 1)[0].rsplit("<a", 1)[1])

    def test_quick_add_from_the_dialog_still_adds(self):
        response = self.client.post(url("quick_add"), {"text": "Gym Mon Wed Fri 6am", "next": url("day")})
        self.assertRedirects(response, url("day"), fetch_redirect_response=False)
        item = ScheduleItem.objects.get(title="Gym")
        self.assertIn("FREQ=WEEKLY", item.recurrence)

    def test_more_options_is_the_full_form_alone(self):
        response = self.client.get(url("new") + "?date=2026-11-06&next=/planner/week/&panel=1")
        self.assertEqual(response.status_code, 200)
        form = response.content.decode()
        self.assertTrue(form.lstrip().startswith('<form method="post" class="pl-form" id="pl-form" novalidate>'))
        for part in ('name="title"', 'name="repeat"', "data-md-editor", 'id="pl-cl-edit-rows"', 'value="2026-11-06"',
                     'name="next" value="/planner/week/"', "data-form-cancel>Cancel</a>"):
            self.assertIn(part, form)
        self.assertNotIn("<html", form)
        self.assertNotIn('class="navbar"', form)
        # ...and the page itself still has the whole thing.
        self.assertIn('class="navbar"', self.client.get(url("new")).content.decode())

    def test_the_form_from_more_options_saves(self):
        response = self.client.post(url("new"), {"title": "Dentist", "kind": "event", "date": "2026-11-06", "repeat": "none",
                                                 "ends": "never", "priority": "none", "next": "/planner/week/"})
        self.assertRedirects(response, "/planner/week/", fetch_redirect_response=False)
        self.assertTrue(ScheduleItem.objects.filter(title="Dentist").exists())

    def test_the_script_wires_the_form_up_inside_the_dialog(self):
        js = static("albright_planner/planner.js")
        more = js[js.index('more.addEventListener("click"'):js.index("// ---------- Keys")]
        self.assertIn('"panel=1"', more)
        self.assertIn("initForm(form);", more)
        self.assertIn("window.MdEditor.init", more)
        self.assertIn("window.location.href = url;", more)                          # fallback: the New item page
        self.assertIn("init: init", static("js/md_editor.js"))


class KeyboardTests(Base):
    def test_d_w_m_t_n(self):
        js = static("albright_planner/planner.js")
        keys = js[js.index("// ---------- Keys"):js.index("// ---------- The item form")]
        self.assertIn('views = {d: "day", w: "week", m: "month"}', keys)
        self.assertIn('key === "t"', keys)
        self.assertIn('key === "n"', keys)
        self.assertIn('event.target.closest("input, textarea, select, [contenteditable]")', keys)   # not while typing
        self.assertIn('document.querySelector("dialog[open]")', keys)                # nor with a dialog open
        self.assertIn("event.metaKey || event.ctrlKey || event.altKey", keys)
