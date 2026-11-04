"""Checklists (subtasks) on every task, repeating ones included: made in the New / Edit form or with quick add
("Task: one; two"), ticked per occurrence (each occurrence starts with every subtask open), edits that apply from
the current occurrence on while past ones keep their history, "2/5" progress in every view and Home's Today panel,
auto-complete per occurrence and "Mark all subtasks done too?", grocery rows, shared editing, and the Bot API."""
import json
from datetime import date

from django.test import Client

from albright_bot_api.models import BotToken
from albright_labs.testing import allow_api_keys
from albright_meals.models import GroceryItem

from . import checklist, planner
from .models import ChecklistItemCompletion, ItemCompletion, ItemShare, PlannerSettings, ScheduleItem, TaskChecklistItem
from .tests import TODAY, Base, url


def d(month, day):
    return date(2026, month, day)


class Fixtures(Base):
    def task(self, title="Pack", items=("Tent", "Stove"), **fields):
        task = self.item(title=title, **fields)
        checklist.create_rows(task, items)
        return task

    def rows(self, task, day):
        """[(text, ticked)] on one occurrence."""
        task.refresh_from_db()
        return [(r.text, r.checked) for r in checklist.occurrence_rows(task, day)]

    def tick(self, task, text, day, client=None):
        row = task.checklist.get(text=text)
        return (client or self.client).post(url("checklist_toggle", task.pk, row.pk), {"date": day.isoformat()},
                                            HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def done(self, task, day):
        return ItemCompletion.objects.filter(item=task, occurrence_date=day).exists()


class CreateTests(Fixtures):
    FORM = {"title": "Pack", "kind": "task", "date": "2026-11-06", "priority": "none", "repeat": "none",
            "ends": "never", "cl_section": "1"}

    def test_form_creates_a_task_with_a_checklist(self):
        self.client.post(url("new"), {**self.FORM, "cl_id": ["", "", "", ""], "cl_text": ["Tent", " Stove ", "", "Lantern"]})
        task = ScheduleItem.objects.get(title="Pack")
        self.assertEqual(list(task.checklist.values_list("text", "order")), [("Tent", 0), ("Stove", 1), ("Lantern", 2)])
        page = self.client.get(url("new")).content.decode()
        self.assertIn('id="pl-cl-edit"', page)
        self.assertIn('data-kind-only="task"', page)

    def test_events_have_no_checklist(self):
        self.client.post(url("new"), {**self.FORM, "kind": "event", "cl_id": [""], "cl_text": ["Tent"]})
        self.assertFalse(TaskChecklistItem.objects.exists())

    def test_quick_add_syntax(self):
        self.client.post(url("quick_add"), {"text": "Pack for camping Fri: tent; stove; lantern"})
        task = ScheduleItem.objects.get()
        self.assertEqual((task.title, task.date, task.kind), ("Pack for camping", d(11, 6), "task"))
        self.assertEqual(list(task.checklist.values_list("text", flat=True)), ["tent", "stove", "lantern"])

    def test_quick_add_without_the_syntax_is_unchanged(self):
        for text in ("Call dentist tomorrow 3:30pm", "Note: call mom", "Read: chapter 3"):
            self.client.post(url("quick_add"), {"text": text})
        self.assertFalse(TaskChecklistItem.objects.exists())
        self.assertEqual(ScheduleItem.objects.get(title__startswith="Call").start_time.minute, 30)


class PerOccurrenceTests(Fixtures):
    def test_daily_ticks_today_not_tomorrow(self):
        task = self.task(recurrence="FREQ=DAILY", recurrence_count=30)
        self.assertEqual(self.tick(task, "Tent", TODAY).json()["progress"], "1/2")
        self.assertEqual(self.rows(task, TODAY), [("Tent", True), ("Stove", False)])
        self.assertEqual(self.rows(task, d(11, 5)), [("Tent", False), ("Stove", False)])

    def test_weekly(self):
        task = self.task(recurrence="FREQ=WEEKLY;BYDAY=WE", recurrence_until=d(12, 31))
        self.tick(task, "Stove", TODAY)
        self.assertEqual(self.rows(task, d(11, 11)), [("Tent", False), ("Stove", False)])
        occs = checklist.attach(planner.occurrences(self.me, d(11, 1), d(11, 30)))
        self.assertEqual([(o.occurrence_date.day, o.checklist_progress["label"]) for o in occs],
                         [(4, "1/2"), (11, "0/2"), (18, "0/2"), (25, "0/2")])

    def test_open_ended_next_occurrence_starts_open(self):
        task = self.task(recurrence="FREQ=DAILY")                     # one at a time
        self.tick(task, "Tent", TODAY)
        self.tick(task, "Stove", TODAY)                               # all ticked: done, on to tomorrow
        self.assertTrue(self.done(task, TODAY))
        self.assertEqual(planner.current_occurrence(ScheduleItem.objects.get(pk=task.pk)), d(11, 5))
        occs = checklist.attach(planner.occurrences(self.me, TODAY, d(11, 6)))
        self.assertEqual([(o.occurrence_date, o.done, o.checklist_progress["label"]) for o in occs],
                         [(TODAY, True, "2/2"), (d(11, 5), False, "0/2")])

    def test_a_tick_is_only_for_a_real_occurrence(self):
        task = self.task(recurrence="FREQ=WEEKLY;BYDAY=WE", recurrence_count=5)
        self.assertEqual(self.tick(task, "Tent", d(11, 5)).status_code, 404)        # a Thursday


class EditingTests(Fixtures):
    def test_edits_apply_from_now_and_past_history_stays(self):
        task = self.task("Laundry", ("Wash", "Dry"), date=d(10, 7), recurrence="FREQ=WEEKLY;BYDAY=WE",
                         recurrence_until=d(12, 30))
        self.tick(task, "Wash", d(10, 28))
        wash, dry = task.checklist.get(text="Wash"), task.checklist.get(text="Dry")
        response = self.client.post(url("edit", task.pk) + "?date=2026-11-04", {
            "title": "Laundry", "kind": "task", "date": "2026-11-04", "priority": "none", "repeat": "weekly",
            "days": ["WE"], "ends": "on", "until": "2026-12-30", "scope": "all", "occurrence": "2026-11-04",
            "cl_section": "1", "cl_id": [str(wash.pk), str(dry.pk), ""], "cl_text": ["Wash darks", "", "Fold"]})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.rows(task, d(10, 28)), [("Wash", True), ("Dry", False)])    # its own history
        self.assertEqual(self.rows(task, TODAY), [("Wash darks", False), ("Fold", False)])
        self.assertEqual(self.rows(task, d(11, 25)), [("Wash darks", False), ("Fold", False)])
        self.assertEqual(ChecklistItemCompletion.objects.get().text, "Wash")

    def test_a_one_off_task_edits_in_place(self):
        task = self.task()
        tent = task.checklist.get(text="Tent")
        self.client.post(url("edit", task.pk), {
            "title": "Pack", "kind": "task", "date": TODAY.isoformat(), "priority": "none", "repeat": "none",
            "ends": "never", "cl_section": "1", "cl_id": ["", str(tent.pk), ""], "cl_text": ["Map", "Tent", ""]})
        self.assertEqual(list(task.checklist.values_list("text", flat=True)), ["Map", "Tent"])

    def test_this_and_following_keeps_the_checklist(self):
        task = self.task(recurrence="FREQ=DAILY", recurrence_count=10, date=d(11, 1))
        self.tick(task, "Tent", d(11, 6))
        new = planner.change(task, d(11, 5), planner.FOLLOWING, {"title": "Pack light"})
        self.assertEqual(self.rows(new, d(11, 6)), [("Tent", True), ("Stove", False)])
        self.assertEqual(self.rows(task, d(11, 2)), [("Tent", False), ("Stove", False)])


class DisplayTests(Fixtures):
    def test_progress_in_every_view_and_the_today_panel(self):
        task = self.task()
        self.tick(task, "Tent", TODAY)
        day = self.client.get(url("day")).content.decode()
        li = day.split(f'data-item="{task.pk}"', 1)[1].split("</details>", 1)[0]
        self.assertIn("data-cl-progress>1/2<", li)
        self.assertIn('data-open-subtasks="1"', day)
        self.assertIn(f'name="date" value="{TODAY.isoformat()}"', li)
        week = self.client.get(url("week")).content.decode()
        self.assertIn("data-cl-progress>1/2<", week.split(f'data-item="{task.pk}"', 1)[1].split("</li>", 1)[0])
        month = self.client.get(url("month")).content.decode()
        self.assertIn('<span class="pl-chip__cl" title="Checklist: 1 of 2 done" data-cl-progress>1/2</span>', month)
        self.assertEqual(planner.today_panel(self.me)["tasks"][0].checklist_progress["label"], "1/2")
        page = self.client.get(url("edit", task.pk)).content.decode()
        self.assertIn('data-cl-progress>1/2</span>', page)


class CompletionTests(Fixtures):
    def test_auto_complete_per_occurrence(self):
        task = self.task(recurrence="FREQ=DAILY", recurrence_count=5)
        self.tick(task, "Tent", TODAY)
        response = self.tick(task, "Stove", TODAY)
        self.assertTrue(response.json()["task_done"])
        self.assertEqual((self.done(task, TODAY), self.done(task, d(11, 5))), (True, False))
        self.assertFalse(self.tick(task, "Stove", TODAY).json()["task_done"])        # unticking reopens it

    def test_auto_complete_off(self):
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"complete_when_checklist_done": False})
        task = self.task()
        self.tick(task, "Tent", TODAY)
        self.tick(task, "Stove", TODAY)
        self.assertFalse(self.done(task, TODAY))

    def test_mark_all_subtasks_done_too(self):
        task = self.task(recurrence="FREQ=DAILY", recurrence_count=5)
        response = self.client.post(url("toggle", task.pk), {"date": TODAY.isoformat(), "all_subtasks": "1"},
                                    HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.json(), {"ok": True, "done": True, "progress": "2/2", "progress_title": "2 of 2 done"})
        self.assertEqual(self.rows(task, TODAY), [("Tent", True), ("Stove", True)])
        self.assertEqual(self.rows(task, d(11, 5)), [("Tent", False), ("Stove", False)])

    def test_just_the_task(self):
        task = self.task()
        self.client.post(url("toggle", task.pk), {"date": TODAY.isoformat()})
        self.assertTrue(self.done(task, TODAY))
        self.assertEqual(self.rows(task, TODAY), [("Tent", False), ("Stove", False)])


class GroceryTests(Fixtures):
    def test_grocery_rows_still_check_the_line(self):
        milk = GroceryItem.objects.create(space=self.kitchen, name="milk", quantity=1, unit="gal")
        task = checklist.task_from_groceries(self.me, [milk], "", TODAY)
        row = task.checklist.get()
        self.client.post(url("checklist_toggle", task.pk, row.pk), {"date": TODAY.isoformat()})
        milk.refresh_from_db()
        self.assertTrue(milk.checked)
        self.assertFalse(ChecklistItemCompletion.objects.exists())
        self.assertTrue(self.done(task, TODAY))


class SharedTests(Fixtures):
    def test_editors_tick_and_it_shows_who(self):
        task = self.task()
        ItemShare.objects.create(item=task, user=self.wife, permission=ItemShare.EDIT)
        wife = self.as_user(self.wife)
        self.assertEqual(self.tick(task, "Tent", TODAY, client=wife).json()["done_by"], "sam")
        self.assertEqual(ChecklistItemCompletion.objects.get().done_by, self.wife)
        day = self.client.get(url("day")).content.decode()
        self.assertIn("Done by sam", day.split(f'data-item="{task.pk}"', 1)[1].split("</details>", 1)[0])

    def test_view_only_cannot_tick(self):
        task = self.task()
        ItemShare.objects.create(item=task, user=self.wife, permission=ItemShare.VIEW)
        self.assertEqual(self.tick(task, "Tent", TODAY, client=self.as_user(self.wife)).status_code, 404)
        self.assertFalse(ChecklistItemCompletion.objects.exists())


class BotApiTests(Fixtures):
    PATH = "/api/bot/v1/schedule"

    def setUp(self):
        super().setUp()
        allow_api_keys(self.me)
        _token, self.raw = BotToken.issue(self.me, "Grok bot", ["schedule:read", "schedule:write"])
        self.api = Client()

    def call(self, path, body=None):
        auth = {"HTTP_AUTHORIZATION": f"Bearer {self.raw}", "secure": True}
        if body is None:
            return self.api.get(path, **auth)
        return self.api.post(path, data=json.dumps(body), content_type="application/json", **auth)

    def test_create_read_and_tick(self):
        created = self.call(self.PATH, {"title": "Pack", "date": "2026-11-04", "repeat": "daily", "count": 3,
                                        "checklist": ["Tent", "Stove"]})
        self.assertEqual(created.status_code, 201, created.content)
        body = created.json()
        self.assertEqual([c["text"] for c in body["checklist"]], ["Tent", "Stove"])
        tent = body["checklist"][0]["id"]
        done = self.call(f"{self.PATH}/{body['id']}/checklist/{tent}/complete", {"occurrence_date": "2026-11-05"})
        self.assertEqual(done.json(), {"item_id": body["id"], "checklist_item_id": tent, "occurrence_date": "2026-11-05",
                                       "done": True, "progress": "1/2", "task_done": False})
        rows = self.call(f"{self.PATH}?start=2026-11-04&end=2026-11-05").json()["occurrences"]
        self.assertEqual([[(c["text"], c["done"]) for c in r["checklist"]] for r in rows],
                         [[("Tent", False), ("Stove", False)], [("Tent", True), ("Stove", False)]])
        undone = self.call(f"{self.PATH}/{body['id']}/checklist/{tent}/uncomplete", {"occurrence_date": "2026-11-05"})
        self.assertFalse(undone.json()["done"])

    def test_rejections(self):
        self.assertEqual(self.call(self.PATH, {"title": "x", "date": "2026-11-04", "kind": "event",
                                               "checklist": ["a"]}).status_code, 400)
        self.assertEqual(self.call(self.PATH, {"title": "x", "date": "2026-11-04", "checklist": "a; b"}).status_code, 400)
        task = self.task(recurrence="FREQ=DAILY", recurrence_count=3)
        row = task.checklist.first()
        self.assertEqual(self.call(f"{self.PATH}/{task.pk}/checklist/{row.pk}/complete", {}).status_code, 400)
        self.assertEqual(self.call(f"{self.PATH}/{task.pk}/checklist/999999/complete",
                                   {"occurrence_date": "2026-11-04"}).status_code, 404)

    def test_docs(self):
        docs = self.call("/api/bot/v1/docs").content.decode()
        self.assertIn("schedule/<id>/checklist/<item_id>/complete", docs)
        self.assertIn('"checklist": ["item", ...]', docs)
