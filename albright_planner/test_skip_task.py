"""Repeating-task skip marks the occurrence skipped (ItemCompletion), keeps the next date on the regular
schedule, does not count as done, and is offered as swipe-to-skip on Day / Home (not 'Skip this one')."""
from datetime import date

from django.urls import reverse

from .models import ItemCompletion, ItemException
from .test_one_at_a_time import Base, d
from . import planner

DAILY = "FREQ=DAILY"
AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def url(name, *args):
    return reverse(f"albright_planner:{name}", args=args)


class SkipOccurrenceTests(Base):
    # Base today is Wed Nov 4.

    def test_marks_skipped_not_an_exception(self):
        item = self.task(title="Make the bed", rule=DAILY, start=d(11, 4))
        self.assertTrue(planner.skip_occurrence(item, d(11, 4), self.me, "travel"))
        completion = ItemCompletion.objects.get(item=item, occurrence_date=d(11, 4))
        self.assertTrue(completion.skipped)
        self.assertEqual(completion.skip_reason, "travel")
        self.assertEqual(completion.skip_label, "Skipped - Travel")
        self.assertFalse(ItemException.objects.filter(item=item).exists())
        self.assertEqual(self.current(item), d(11, 5))
        shown = {o.date: o for o in planner.occurrences(self.me, d(11, 4), d(11, 5)) if o.item.pk == item.pk}
        self.assertTrue(shown[d(11, 4)].skipped)
        self.assertFalse(shown[d(11, 5)].done)

    def test_from_completion_does_not_step_from_the_skip_date(self):
        item = self.task(title="Walk", rule=DAILY, start=d(11, 3), repeat_from_completion=True)
        # If skip counted as done, next would be Nov 6 (every day after Nov 5's check-off clock).
        # Skip on Nov 4 (today; current is Nov 3 if never done? start Nov 3, today Nov 4:
        # current is Nov 3 still if not done). Skip the current one (Nov 3).
        self.assertEqual(self.current(item), d(11, 3))
        planner.skip_occurrence(item, d(11, 3), self.me, "travel")
        # Regular daily schedule: next is Nov 4, not Nov 5 (step_from after a "done" on Nov 3 or 4).
        self.assertEqual(self.current(item), d(11, 4))

    def test_already_done_cannot_be_skipped(self):
        item = self.task(rule=DAILY, start=d(11, 4))
        self.done(item, d(11, 4))
        with self.assertRaises(planner.SkipError):
            planner.skip_occurrence(item, d(11, 4), self.me)
        self.assertFalse(ItemCompletion.objects.get(item=item).skipped)

    def test_one_off_cannot_be_skipped(self):
        item = self.task(rule="", start=d(11, 4))
        with self.assertRaises(planner.SkipError):
            planner.skip_occurrence(item, d(11, 4), self.me)

    def test_undo_reopens_and_puts_it_back(self):
        item = self.task(rule=DAILY, start=d(11, 4))
        planner.skip_occurrence(item, d(11, 4), self.me, "busy")
        self.assertEqual(self.current(item), d(11, 5))
        self.assertFalse(planner.toggle(item, d(11, 4), self.me, done=False))
        self.assertEqual(self.current(item), d(11, 4))
        self.assertFalse(item.completions.exists())

    def test_skipped_is_not_counted_as_done_on_today(self):
        item = self.task(title="Make the bed", rule=DAILY, start=d(11, 4))
        other = self.task(title="Trash", rule=DAILY, start=d(11, 4))
        planner.skip_occurrence(item, d(11, 4), self.me, "travel")
        self.done(other, d(11, 4))
        panel = planner.today_panel(self.me, show_completed=True)
        titles = [o.title for o in panel["tasks"]]
        self.assertIn("Make the bed", titles)
        skipped = next(o for o in panel["tasks"] if o.title == "Make the bed")
        self.assertTrue(skipped.skipped)
        self.assertEqual(panel["done"], 1)          # only Trash
        self.assertEqual(panel["open"], 0)


class SkipViewTests(Base):
    def test_post_skips_with_a_reason(self):
        item = self.task(title="Make the bed", rule=DAILY, start=d(11, 4))
        response = self.client.post(url("skip", item.pk), {
            "date": "2026-11-04", "reason": "travel", "next": "/planner/day/",
        }, **AJAX)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["skipped"])
        self.assertTrue(ItemCompletion.objects.get(item=item).skipped)

    def test_undo_from_the_view(self):
        item = self.task(rule=DAILY, start=d(11, 4))
        planner.skip_occurrence(item, d(11, 4), self.me, "travel")
        response = self.client.post(url("skip", item.pk), {"date": "2026-11-04", "undo": "1"}, **AJAX)
        self.assertEqual(response.json(), {"ok": True, "skipped": False})
        self.assertFalse(item.completions.exists())

    def test_day_page_has_swipe_and_no_skip_this_one(self):
        item = self.task(title="Make the bed", rule=DAILY, start=d(11, 4))
        page = self.client.get(url("day")).content.decode()
        block = page.split(f'data-item="{item.pk}"', 1)[1][:2500]
        self.assertIn("data-skip-swipe", page.split(f'data-item="{item.pk}"', 1)[0][-200:] + block[:200])
        self.assertIn("pl-swipe", page)
        self.assertNotIn("Skip this one", page)
        self.assertIn("skip-pop", block)

    def test_view_panel_menu_uses_the_skip_form(self):
        item = self.task(title="Make the bed", rule=DAILY, start=d(11, 4), recurrence_until=d(12, 31))
        page = self.client.get(url("item", item.pk) + "?panel=1&date=2026-11-04").content.decode()
        self.assertIn('id="pl-view-skip"', page)
        self.assertIn("skip-pop", page)
        self.assertNotIn("Skip this one", page)
        self.assertNotIn('scope" value="this"', page)
