"""TEMPORARY (deleted before commit): the quick view's autosave and the edit page's JSON Save."""
import re
from datetime import time

from albright_notes.models import Note

from . import checklist, recurrence
from .models import ItemException
from .tests import TODAY, Base, d, url

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


class QuickTests(Base):
    def quick(self, item, **data):
        data.setdefault("date", TODAY.isoformat())
        return self.client.post(url("quick_edit", item.pk), data, **AJAX)

    def test_fields(self):
        task = self.item("Plan", kind="task", checkable=True)
        r = self.quick(task, field="title", value="Business plan")
        self.assertEqual(r.status_code, 200, r.content)
        task.refresh_from_db()
        self.assertEqual(task.title, "Business plan")
        r = self.quick(task, field="date", value=d(11, 6).isoformat()).json()
        task.refresh_from_db()
        self.assertEqual((task.date, r["occurrence"], r["date"]), (d(11, 6), "2026-11-06", "Fri, Nov 6"))
        r = self.quick(task, date="2026-11-06", field="time", start="09:00", end="10:30").json()
        task.refresh_from_db()
        self.assertEqual((task.start_time, r["time"]), (time(9, 0), "9:00 AM–10:30 AM"))
        bad = self.quick(task, date="2026-11-06", field="time", start="11:00", end="10:00")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("after the start", bad.json()["error"])
        r = self.quick(task, date="2026-11-06", field="time", start="", end="").json()
        self.assertEqual(r["time"], "All day")
        r = self.quick(task, date="2026-11-06", field="notes", value="**hi**").json()
        self.assertIn("<strong>hi</strong>", r["notes_html"])
        r = self.quick(task, date="2026-11-06", field="repeat", value="weekly").json()
        task.refresh_from_db()
        self.assertTrue(task.recurrence.startswith("FREQ=WEEKLY"))
        self.assertIn("Fri", r["repeat"])
        r = self.quick(task, date="2026-11-06", field="cl_add", value="Outline").json()
        self.assertIn("Outline", r["row_html"])
        row = task.checklist.get()
        self.quick(task, date="2026-11-06", field="cl_text", row=row.pk, value="Outline v2")
        self.assertEqual(task.checklist.get().text, "Outline v2")
        self.quick(task, date="2026-11-06", field="cl_text", row=row.pk, value="")
        self.assertEqual(list(checklist.current_rows(task)), [])

    def test_note_link(self):
        task = self.item("Trip", kind="task")
        note = Note.objects.create(owner=self.me, title="Packing", body="x")
        r = self.quick(task, field="note", value=str(note.pk)).json()
        task.refresh_from_db()
        self.assertEqual((task.note_id, r["note"]), (note.pk, "Packing"))
        self.quick(task, field="note", value="none")
        task.refresh_from_db()
        self.assertIsNone(task.note_id)

    def test_repeating_date_moves_one_occurrence(self):
        series = self.item("Walk", kind="task", recurrence=recurrence.build("daily", TODAY))
        r = self.quick(series, field="date", value=d(11, 8).isoformat())
        self.assertEqual(r.status_code, 200, r.content)
        exc = ItemException.objects.get(item=series, occurrence_date=TODAY)
        self.assertEqual(exc.new_date, d(11, 8))
        series.refresh_from_db()
        self.assertEqual(series.date, TODAY)

    def test_others_cannot(self):
        task = self.item("Mine", kind="task")
        r = self.as_user(self.stranger).post(url("quick_edit", task.pk), {"field": "title", "value": "x"}, **AJAX)
        self.assertEqual(r.status_code, 404)

    def test_panel_has_rows_and_no_add_files(self):
        task = self.item("Plan", kind="task")
        page = self.client.get(url("item", task.pk) + "?panel=1").content.decode()
        self.assertIn('data-quick-url="/planner/%d/quick/"' % task.pk, page)
        self.assertIn("Linked note", page)
        self.assertNotIn("data-att-form", page)
        self.assertNotIn("Add files", page)


class EditSaveTests(Base):
    def form(self, item, **over):
        data = {"title": item.title, "kind": "task", "date": item.date.isoformat(), "priority": "none", "repeat": "none",
                "ends": "never", "occurrence": item.date.isoformat(), "cl_section": "1", "note_choice": "keep", "next": "/planner/"}
        data.update(over)
        return data

    def test_json_save_stays_and_returns_rows(self):
        task = self.item("Plan", kind="task", checkable=True)
        r = self.client.post(url("edit", task.pk), self.form(task, title="Plan 2", cl_id=["", ""], cl_text=["A", ""],
                                                             note_choice="new"), **AJAX)
        self.assertEqual(r.status_code, 200, r.content)
        data = r.json()
        self.assertTrue(data["ok"])
        task.refresh_from_db()
        self.assertEqual(task.title, "Plan 2")
        row = task.checklist.get()
        self.assertIn('name="cl_id" value="%d"' % row.pk, data["checklist_html"])
        self.assertIsNotNone(task.note_id)
        self.assertEqual(data["note"], "Plan 2")
        r = self.client.post(url("edit", task.pk), self.form(task, start_time="11:00", end_time="10:00"), **AJAX)
        self.assertEqual(r.status_code, 400)
        self.assertIn("end_time", r.json()["errors"])

    def test_plain_post_stays_on_the_page(self):
        task = self.item("Plan", kind="task")
        r = self.client.post(url("edit", task.pk), self.form(task, title="Plan 3"))
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r["Location"].startswith(url("edit", task.pk)))

    def test_edit_page_renders(self):
        task = self.item("Plan", kind="task")
        page = self.client.get(url("edit", task.pk)).content.decode()
        for bit in ('id="pl-save"', 'data-edit-form', 'id="pl-unsaved"', 'data-share-open', 'name="note_choice"',
                    'class="pl-ed__title"', 'form="pl-form"'):
            self.assertIn(bit, page)


class InstantTests(Base):
    def test_rows_carry_their_quick_view(self):
        import html as htmlmod
        import json
        task = self.item("Pack", kind="task", checkable=True, notes="bring **tent**")
        checklist.create_rows(task, ["stove", "matches"])
        page = self.client.get(url("day") + f"?date={TODAY.isoformat()}").content.decode()
        raw = re.search(r'data-qv="([^"]+)"', page).group(1)
        data = json.loads(htmlmod.unescape(raw))
        self.assertEqual((data["id"], data["title"], data["timeText"], data["shared"], data["note"]),
                         (task.pk, "Pack", "All day", "Only me", "None"))
        self.assertEqual([r["t"] for r in data["checklist"]], ["stove", "matches"])
        self.assertIn("tent", data["notes"])

    def test_panel_is_lean_and_cached_until_something_changes(self):
        task = self.item("Pack", kind="task", checkable=True)
        self.client.get(url("day"))                                    # a CSRF cookie, like a real browser
        path = url("item", task.pk) + f"?date={TODAY.isoformat()}&panel=1"
        first = self.client.get(path).content.decode()
        self.assertNotIn('id="share-box"', first)                       # the Share control loads when its row opens
        self.assertIn(url("share_box", task.pk), first)
        self.assertIn(url("note_choices", task.pk), first)
        self.assertEqual(self.client.get(path).content.decode(), first)
        self.client.post(url("toggle", task.pk), {"date": TODAY.isoformat(), "checked": "1"}, **AJAX)
        after = self.client.get(path).content.decode()
        self.assertNotEqual(after, first)
        self.assertIn('aria-checked="true"', after)
        self.assertIn('id="share-box"', self.client.get(url("share_box", task.pk)).content.decode())
        self.assertEqual(self.client.get(url("note_choices", task.pk)).json(), {"notes": []})
        self.assertEqual(self.as_user(self.stranger).get(url("share_box", task.pk)).status_code, 404)
