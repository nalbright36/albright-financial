"""Option A week layout: Briefing day-column cards, + Add per day, dailies still in every column."""
import pathlib
import re
from datetime import time, timedelta

from django.contrib.staticfiles import finders

from .models import ScheduleItem
from .tests import TODAY, Base, d, url


def static(path):
    return pathlib.Path(finders.find(path)).read_text(encoding="utf-8")


def week_page(client, date="2026-11-04"):
    return client.get(url("week") + f"?date={date}").content.decode()


class WeekLayoutTests(Base):
    def test_week_uses_briefing_column_classes(self):
        self.item(title="Gym", date=d(11, 4))
        page = week_page(self.client)
        self.assertIn('class="pl-week rb-strip"', page)
        self.assertIn('id="pl-week"', page)
        cols = re.findall(r'<section class="pl-col rb-col(?: is-today)?" data-drop-date="([^"]+)"', page)
        self.assertEqual(cols, [(d(11, 2) + timedelta(days=i)).isoformat() for i in range(7)])
        self.assertIn('class="pl-col rb-col is-today"', page)          # Wednesday the 4th
        self.assertIn('class="pl-col__head rb-col__head"', page)
        self.assertIn('draggable="true"', page)
        self.assertIn('id="pl-scope"', page)

    def test_each_day_has_an_add_link_for_that_date(self):
        page = week_page(self.client)
        for i in range(7):
            day = (d(11, 2) + timedelta(days=i)).isoformat()
            self.assertIn(f'class="pl-col__add" data-date="{day}"', page)
            self.assertIn(f'href="{url("new")}?date={day}&amp;next=', page)
        self.assertEqual(page.count('class="pl-col__add"'), 7)

    def test_empty_day_says_free_day(self):
        page = week_page(self.client)
        self.assertIn("Free day", page)
        self.assertNotIn("&mdash;", page)

    def test_a_daily_item_still_appears_in_every_column(self):
        self.item(title="Take supplements", recurrence="FREQ=DAILY", date=d(11, 2), recurrence_count=30)
        page = week_page(self.client)
        cols = re.findall(r'data-drop-date="(\d{4}-\d{2}-\d{2})"', page)
        by_col = {c: page.split(f'data-drop-date="{c}"', 1)[1].split("</section>", 1)[0] for c in cols}
        for col in cols:
            titles = re.findall(r'class="pl-item__title(?: rb-it__title)?">([^<]+)<', by_col[col])
            self.assertEqual(titles, ["Take supplements"])

    def test_timed_event_and_task_keep_check_off_and_tap_to_edit(self):
        self.item(title="Dentist", date=d(11, 5), start_time=time(15), kind="event", checkable=False)
        task = self.item(title="Groceries", date=d(11, 5))
        page = week_page(self.client)
        thu = page.split('data-drop-date="2026-11-05"', 1)[1].split("</section>", 1)[0]
        self.assertIn("rb-it--event", thu)
        self.assertIn("rb-it--task", thu)
        self.assertIn("pl-check", thu)                                 # Groceries can be checked off
        self.assertIn(f'href="{url("item", task.pk)}?date=2026-11-05', thu)
        self.assertIn('data-view', thu)
        self.assertIn("3:00", thu)                                     # time still shown

    def test_quick_add_uses_the_posted_date(self):
        # + Add on Saturday, text with no date of its own: lands on Saturday, not today (Wed).
        response = self.client.post(url("quick_add"), {
            "text": "Buy milk", "date": "2026-11-07", "next": url("week"),
        })
        self.assertRedirects(response, url("week"), fetch_redirect_response=False)
        item = ScheduleItem.objects.get()
        self.assertEqual((item.title, item.date), ("Buy milk", d(11, 7)))

    def test_quick_add_without_a_date_still_uses_today(self):
        self.client.post(url("quick_add"), {"text": "Buy milk", "next": url("week")})
        self.assertEqual(ScheduleItem.objects.get().date, TODAY)

    def test_a_date_in_the_text_still_wins(self):
        self.client.post(url("quick_add"), {
            "text": "Dentist tomorrow 3pm", "date": "2026-11-07", "next": url("week"),
        })
        item = ScheduleItem.objects.get()
        self.assertEqual((item.title, item.date, item.start_time), ("Dentist", d(11, 8), time(15)))

    def test_script_opens_add_in_the_new_item_dialog_on_that_date(self):
        js = static("albright_planner/planner.js")
        self.assertIn("setNewDate", js)
        self.assertIn('event.target.closest(".pl-col__add")', js)
        self.assertIn('getElementById("pl-quick-date")', js)
        self.assertIn("pl-col__add", static("albright_planner/planner.css"))
