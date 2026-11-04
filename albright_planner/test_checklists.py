"""Planner tasks made from Meals' grocery list: "Create task" from the picked lines, a checklist whose grocery rows
ARE the lines' checked boxes (checking either side shows on the other), "3/7" in Day / Week / the task
page, auto-complete and reopen (with the setting), "In task" chips, cleared lines still in the checklist, and who
may check what."""
from datetime import time

from django.contrib.auth.models import User
from django.urls import reverse

from albright_labs.testing import kitchen, with_apps
from albright_meals.models import GroceryItem

from . import checklist
from .models import ChecklistItemCompletion, ItemCompletion, ItemShare, PlannerSettings, ScheduleItem, TaskChecklistItem
from .tests import PASSWORD, TODAY, Base, url


class Fixtures(Base):
    def setUp(self):
        super().setUp()
        self.milk = GroceryItem.objects.create(household=self.kitchen, name="milk", quantity=1, unit="gal", aisle="dairy")
        self.eggs = GroceryItem.objects.create(household=self.kitchen, name="eggs", quantity=12, aisle="dairy")
        self.bread = GroceryItem.objects.create(household=self.kitchen, name="bread", aisle="bakery")

    def create(self, *lines, client=None, **fields):
        data = {"item": [str(l.pk) for l in lines], "title": "", "date": TODAY.isoformat(), **fields}
        return (client or self.client).post(url("from_groceries"), data, follow=True)

    def task(self):
        return ScheduleItem.objects.get(title__icontains="grocery") if ScheduleItem.objects.count() != 1 \
            else ScheduleItem.objects.get()

    def toggle_line(self, line, client=None):
        return (client or self.client).post(reverse("albright_meals:grocery_toggle", args=[line.pk]))

    def toggle_row(self, row, client=None):
        return (client or self.client).post(url("checklist_toggle", row.task_id, row.pk))

    def done(self, task):
        return ItemCompletion.objects.filter(item=task, occurrence_date=task.date).exists()


class CreateTaskTests(Fixtures):
    def test_create_task_from_selected_items(self):
        response = self.create(self.milk, self.bread, time="17:30", note="Costco")
        self.assertContains(response, "Created “Grocery run” for Wed Nov 4 with 2 items.")
        task = ScheduleItem.objects.get()
        self.assertEqual((task.owner, task.title, task.date, task.start_time, task.notes, task.kind, task.checkable),
                         (self.me, "Grocery run", TODAY, time(17, 30), "Costco", "task", True))
        rows = list(task.checklist.all())
        self.assertEqual([(r.text, r.grocery_item_id) for r in rows],
                         [("bread", self.bread.pk), ("1 gal milk", self.milk.pk)])   # in list (aisle) order
        self.assertEqual(task.checklist.first().order, 0)

    def test_title_date_and_items_are_checked(self):
        self.create(self.eggs, title="Saturday shop", date="2026-11-07")
        self.assertEqual(ScheduleItem.objects.get().title, "Saturday shop")
        response = self.create()
        self.assertContains(response, "Pick at least one item for the task.")
        response = self.create(self.eggs, date="")
        self.assertContains(response, "Pick a date for the task")
        self.assertEqual(ScheduleItem.objects.count(), 1)

    def test_needs_meals_as_well_as_the_planner(self):
        self.assertEqual(self.create(self.milk, client=self.as_user(self.wife)).status_code, 404)    # Planner only
        meals_only = with_apps(User.objects.create_user(username="cook", password=PASSWORD), ["meals"])
        self.assertEqual(self.create(self.milk, client=self.as_user(meals_only)).status_code, 404)  # no Planner
        page = self.as_user(meals_only).get(reverse("albright_meals:groceries")).content.decode()
        self.assertNotIn('id="grocery-task-form"', page)                    # so no Select / Create task either
        self.assertIn('id="grocery-task-form"', self.client.get(reverse("albright_meals:groceries")).content.decode())


class BothWaysTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.create(self.milk, self.eggs, self.bread)
        self.task_obj = ScheduleItem.objects.get()
        self.rows = {r.grocery_item_id: r for r in self.task_obj.checklist.all()}

    def test_checking_in_the_task_checks_the_grocery_list(self):
        self.toggle_row(self.rows[self.milk.pk])
        self.milk.refresh_from_db()
        self.assertTrue(self.milk.checked)
        self.assertEqual(self.milk.checked_by, self.me)
        html = self.client.get(reverse("albright_meals:grocery_list")).json()["html"]
        self.assertIn('class="meals-item is-checked" data-item="%d"' % self.milk.pk, html)
        self.assertFalse(ChecklistItemCompletion.objects.exists())       # nothing stored on the row: it's the line

    def test_checking_on_the_grocery_list_shows_in_the_task(self):
        self.toggle_line(self.eggs)
        row = TaskChecklistItem.objects.select_related("grocery_item").get(pk=self.rows[self.eggs.pk].pk)
        self.assertTrue({r.pk: r.checked for r in checklist.occurrence_rows(self.task_obj, TODAY)}[row.pk])
        page = self.client.get(url("edit", self.task_obj.pk)).content.decode()
        self.assertIn("1 of 3 bought", page)
        eggs_row = page.split(f'data-row="{row.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn('aria-checked="true"', eggs_row)

    def test_auto_complete_and_reopen(self):
        for line in (self.milk, self.eggs):
            self.toggle_line(line)
        self.assertFalse(self.done(self.task_obj))
        self.toggle_row(self.rows[self.bread.pk])                           # the last one, from the task
        self.assertTrue(self.done(self.task_obj))
        self.toggle_line(self.eggs)                                          # unchecked on the grocery list
        self.assertFalse(self.done(self.task_obj))
        self.toggle_line(self.eggs)
        self.assertTrue(self.done(self.task_obj))

    def test_auto_complete_can_be_turned_off(self):
        self.client.post(url("settings"), {"show_meals": "1"})              # the box unticked
        self.assertFalse(PlannerSettings.objects.get(user=self.me).complete_when_checklist_done)
        for line in (self.milk, self.eggs, self.bread):
            self.toggle_line(line)
        self.assertFalse(self.done(self.task_obj))
        self.client.post(url("settings"), {"show_meals": "1", "complete_when_checklist_done": "1"})
        self.assertTrue(PlannerSettings.objects.get(user=self.me).complete_when_checklist_done)

    def test_a_task_made_from_lines_already_bought_starts_done(self):
        GroceryItem.objects.filter(pk=self.milk.pk).update(checked=True)
        self.create(self.milk, title="Done already")
        self.assertTrue(self.done(ScheduleItem.objects.get(title="Done already")))

    def test_day_week_and_task_page_show_progress(self):
        self.toggle_line(self.milk)
        day = self.client.get(url("day") + f"?date={TODAY}").content.decode()
        item = day.split(f'data-item="{self.task_obj.pk}"', 1)[1].split('</details>', 1)[0]
        self.assertIn("1 of 3 bought", item)
        self.assertIn("1 gal milk", item)
        self.assertIn(url("checklist_toggle", self.task_obj.pk, self.rows[self.eggs.pk].pk), item)
        week = self.client.get(url("week") + f"?date={TODAY}").content.decode()
        li = week.split(f'data-item="{self.task_obj.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn("1 of 3 bought", li)
        self.assertIn('data-cl-toggle aria-expanded="false"', li)        # Week too: the count, its chevron...
        self.assertRegex(li, r'<ul class="pl-cl__rows pl-item__cl" id="pl-cl-\d+-\d+" hidden>')   # ...rows folded away
        page = self.client.get(url("edit", self.task_obj.pk)).content.decode()
        self.assertIn('id="pl-cl-progress" title="1 of 3 bought" data-cl-progress>1/3', page)

    def test_in_task_chip_on_the_grocery_list(self):
        html = self.client.get(reverse("albright_meals:grocery_list")).json()["html"]
        milk = html.split(f'data-item="{self.milk.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn("In task: Grocery run (Wed)", milk)
        self.assertIn(url("item", self.task_obj.pk) + "?next=" + reverse("albright_meals:groceries"),
                      milk)                                            # its view panel, Close back to the list


class ArchiveTests(Fixtures):
    def test_cleared_lines_stay_in_the_task_checklist(self):
        self.create(self.milk, self.eggs)
        task = ScheduleItem.objects.get()
        self.toggle_line(self.milk)
        self.client.post(reverse("albright_meals:grocery_clear"))
        self.assertTrue(GroceryItem.objects.filter(pk=self.milk.pk, archived_at__isnull=False).exists())
        html = self.client.get(reverse("albright_meals:grocery_list")).json()["html"]
        self.assertNotIn(f'data-item="{self.milk.pk}"', html)
        page = self.client.get(url("edit", task.pk)).content.decode()
        self.assertIn("1 gal milk", page)
        self.assertIn("cleared from the list", page)
        self.assertIn("1 of 2 bought", page)

    def test_a_deleted_line_keeps_its_last_state_on_the_row(self):
        self.create(self.milk, self.eggs)
        self.toggle_line(self.milk)
        self.client.post(reverse("albright_meals:grocery_delete", args=[self.milk.pk]))
        row = TaskChecklistItem.objects.get(text="1 gal milk")
        self.assertIsNone(row.grocery_item_id)
        self.assertTrue({r.pk: r.checked for r in checklist.occurrence_rows(row.task, TODAY)}[row.pk])   # a tick now

    def test_merging_a_duplicate_moves_its_checklist_row(self):
        from albright_meals import groceries
        cup = GroceryItem.objects.create(household=self.kitchen, name="milk", quantity=2, unit="cup", duplicate_of=self.milk)
        self.create(cup)
        groceries.merge(cup)
        self.assertEqual(TaskChecklistItem.objects.get().grocery_item, self.milk)


class PermissionTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.create(self.milk, self.eggs)
        self.task_obj = ScheduleItem.objects.get()
        self.row = self.task_obj.checklist.get(grocery_item=self.milk)
        self.cook = with_apps(User.objects.create_user(username="cook", password=PASSWORD), ["meals", "planner"])
        kitchen(self.me, self.cook)                                         # the grocery list is the household's

    def test_anyone_with_the_grocery_list_can_check_and_it_shows_in_the_task(self):
        self.toggle_line(self.milk, client=self.as_user(self.cook))
        self.assertIn("1 of 2 bought", self.client.get(url("edit", self.task_obj.pk)).content.decode())

    def test_the_task_stays_private(self):
        cook = self.as_user(self.cook)
        self.assertEqual(cook.get(url("edit", self.task_obj.pk)).status_code, 404)
        self.assertEqual(self.toggle_row(self.row, client=cook).status_code, 404)
        html = cook.get(reverse("albright_meals:grocery_list")).json()["html"]
        self.assertNotIn("In task:", html)                                   # someone else's task: no chip
        self.assertNotIn("Grocery run", html)
        ItemShare.objects.create(item=self.task_obj, user=self.cook, permission=ItemShare.VIEW)
        self.assertIn("In task: Grocery run", cook.get(reverse("albright_meals:grocery_list")).json()["html"])
        self.assertEqual(self.toggle_row(self.row, client=cook).status_code, 404)   # view only
        page = cook.get(url("edit", self.task_obj.pk)).content.decode()
        self.assertNotIn(url("checklist_toggle", self.task_obj.pk, self.row.pk), page)

    def test_editors_check_rows_but_grocery_rows_need_meals(self):
        ItemShare.objects.create(item=self.task_obj, user=self.cook, permission=ItemShare.EDIT)
        self.assertEqual(self.toggle_row(self.row, client=self.as_user(self.cook)).status_code, 302)
        self.milk.refresh_from_db()
        self.assertTrue(self.milk.checked)
        ItemShare.objects.create(item=self.task_obj, user=self.wife, permission=ItemShare.EDIT)   # Planner only
        wife = self.as_user(self.wife)
        self.assertEqual(self.toggle_row(self.task_obj.checklist.get(grocery_item=self.eggs), client=wife).status_code, 404)
        page = wife.get(url("edit", self.task_obj.pk)).content.decode()
        self.assertIn("1 gal milk", page)
        self.assertNotIn(url("checklist_toggle", self.task_obj.pk, self.row.pk), page)

    def test_grocery_rows_are_read_only_outside_the_household(self):
        outsider = with_apps(User.objects.create_user(username="outsider", password=PASSWORD), ["meals", "planner"])
        kitchen(outsider, name="Elsewhere")
        ItemShare.objects.create(item=self.task_obj, user=outsider, permission=ItemShare.EDIT)
        self.assertEqual(self.toggle_row(self.row, client=self.as_user(outsider)).status_code, 404)
        self.milk.refresh_from_db()
        self.assertFalse(self.milk.checked)


class ProgressTests(Fixtures):
    def test_progress_wording(self):
        task = ScheduleItem.objects.create(owner=self.me, title="Pack", date=TODAY)
        rows = [TaskChecklistItem.objects.create(task=task, text="Tent"),
                TaskChecklistItem.objects.create(task=task, text="Stove")]
        checklist.set_checked(rows[0], TODAY, True, self.me)
        shown = checklist.progress(checklist.occurrence_rows(task, TODAY))
        self.assertEqual((shown["label"], shown["title"]), ("1/2", "1 of 2 done"))
        checklist.set_checked(rows[1], TODAY, True, self.me)
        self.assertTrue(self.done(task))
        checklist.set_checked(rows[0], TODAY, False, self.me)
        self.assertFalse(self.done(task))
