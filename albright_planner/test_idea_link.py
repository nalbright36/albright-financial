"""Planner tasks for an Admin Idea (ScheduleItem.idea): several per Idea (each its own item and date - the
one-item-per-source constraint doesn't apply), listed on the Idea page with done/open for that occurrence, only
ever the viewer's own, unlinked when the Idea is deleted, and a gear on the Planner row linking back."""
from datetime import timedelta

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from albright_control.models import Idea
from albright_labs.testing import with_apps

from .models import ItemCompletion, ScheduleItem

PASSWORD = "pw-not-real-12345"


class IdeaTaskTests(TestCase):
    def setUp(self):
        self.me = with_apps(User.objects.create_superuser(username="boss", password=PASSWORD, email=""))
        self.other = with_apps(User.objects.create_superuser(username="other", password=PASSWORD, email=""))
        self.client.force_login(self.me)
        self.idea = Idea.objects.create(title="Dark charts", description="d", original_message="m")
        self.page_url = reverse("albright_control:idea", args=[self.idea.pk])
        self.today = timezone.localdate()

    def add(self, title, day, notes=""):
        return self.client.post(self.page_url, {"action": "add_task", "title": title, "date": day.isoformat(),
                                                "notes": notes})

    def two_tasks(self):
        self.add("Sketch the dark palette", self.today)
        self.add("Ship dark charts", self.today + timedelta(days=3), notes="after review")
        return list(ScheduleItem.objects.filter(idea=self.idea).order_by("date"))

    def test_two_tasks_for_one_idea(self):
        response = self.add("Sketch the dark palette", self.today)
        self.assertRedirects(response, self.page_url, fetch_redirect_response=False)
        self.add("Ship dark charts", self.today + timedelta(days=3), notes="after review")
        first, second = ScheduleItem.objects.filter(idea=self.idea).order_by("date")
        for item in (first, second):
            self.assertEqual((item.owner, item.kind, item.idea_id, item.source_url, item.source_app, item.source_id),
                             (self.me, ScheduleItem.TASK, self.idea.pk, f"/control/ideas/{self.idea.pk}/", "", ""))
            self.assertFalse(item.locked)
        self.assertEqual((second.title, second.notes, second.date),
                         ("Ship dark charts", "after review", self.today + timedelta(days=3)))
        self.assertEqual(list(self.idea.planner_tasks.order_by("date")), [first, second])

    def test_form_defaults_to_the_idea_title_and_today(self):
        response = self.client.get(self.page_url)
        form = response.context["task_form"]
        self.assertEqual((form.initial["title"], form.initial["date"]), ("Dark charts", self.today))
        self.assertContains(response, ">Add Planner task</button>")

    def test_idea_page_lists_titles_and_done(self):
        first, second = self.two_tasks()
        ItemCompletion.objects.create(item=first, occurrence_date=first.date, completed_by=self.me)
        response = self.client.get(self.page_url)
        rows = {row["item"].title: row["done"] for row in response.context["planner_tasks"]}
        self.assertEqual(rows, {"Sketch the dark palette": True, "Ship dark charts": False})
        content = response.content.decode()
        section = content.split('id="idea-planner-tasks"', 1)[1].split("</section>", 1)[0]
        for item, label in ((first, "Done"), (second, "Open")):
            row = section.split(f'data-item="{item.pk}"', 1)[1].split("</li>", 1)[0]
            self.assertIn(f">{label}</span>", row)
            self.assertIn(item.title, row)
            self.assertIn(reverse("albright_planner:item", args=[item.pk]), row)

    def test_archived_tasks_are_left_out(self):
        first, second = self.two_tasks()
        ScheduleItem.objects.filter(pk=second.pk).update(archived=True)
        titles = [row["item"].title for row in self.client.get(self.page_url).context["planner_tasks"]]
        self.assertEqual(titles, [first.title])

    def test_another_user_never_sees_my_idea_tasks(self):
        first, second = self.two_tasks()
        client = Client()
        client.force_login(self.other)
        response = client.get(self.page_url)
        self.assertEqual(response.context["planner_tasks"], [])
        self.assertNotContains(response, "Sketch the dark palette")
        self.assertNotContains(response, "Ship dark charts")
        self.assertFalse(ScheduleItem.objects.visible_to(self.other).filter(pk__in=[first.pk, second.pk]).exists())
        self.assertEqual(ScheduleItem.objects.visible_to(self.me).filter(idea=self.idea).count(), 2)
        # Their own task for the same Idea shows only to them.
        client.post(self.page_url, {"action": "add_task", "title": "Their take", "date": self.today.isoformat()})
        self.assertNotContains(self.client.get(self.page_url), "Their take")
        self.assertContains(client.get(self.page_url), "Their take")

    def test_deleting_the_idea_unlinks_the_tasks(self):
        first, second = self.two_tasks()
        self.client.post(self.page_url, {"action": "delete"})
        self.assertFalse(Idea.objects.filter(pk=self.idea.pk).exists())
        for item in (first, second):
            item.refresh_from_db()
            self.assertEqual((item.idea_id, item.source_url), (None, ""))

    def test_deleting_a_task_keeps_the_idea(self):
        first, second = self.two_tasks()
        first.delete()
        self.assertTrue(Idea.objects.filter(pk=self.idea.pk).exists())
        self.assertEqual(list(self.idea.planner_tasks.all()), [second])

    def test_gear_on_planner_rows_links_to_the_idea(self):
        first, _second = self.two_tasks()
        content = self.client.get(reverse("albright_planner:day") + f"?date={self.today.isoformat()}").content.decode()
        row = content.split(f'data-item="{first.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn(f'<a href="/control/ideas/{self.idea.pk}/" class="pl-src pl-idea" title="Open its Idea"', row)
        self.assertNotIn("&#x2197;", row)                                  # the gear instead of the plain link
        self.assertIn(reverse("albright_planner:toggle", args=[first.pk]), row)   # not locked: still checkable

    def test_add_from_the_list_goes_back_to_the_list(self):
        back = "/control/ideas/?status=new"
        response = self.client.post(self.page_url, {"action": "add_task", "title": "From the list",
                                                    "date": self.today.isoformat(), "next": back})
        self.assertRedirects(response, back, fetch_redirect_response=False)
        item = ScheduleItem.objects.get(title="From the list")
        self.assertEqual((item.idea_id, item.owner), (self.idea.pk, self.me))

    def test_offsite_next_goes_to_the_idea_page(self):
        response = self.client.post(self.page_url, {"action": "add_task", "title": "Elsewhere",
                                                    "date": self.today.isoformat(), "next": "https://evil.example/"})
        self.assertRedirects(response, self.page_url, fetch_redirect_response=False)
        self.assertTrue(ScheduleItem.objects.filter(idea=self.idea, title="Elsewhere").exists())

    # Steps in the ideas list --------------------------------------------------
    def steps_panel(self, client=None):
        content = (client or self.client).get(reverse("albright_control:ideas")).content.decode()
        detail = content.split(f'id="rl-detail-{self.idea.pk}"', 1)[1].split("</section>", 1)[0]
        return detail.split('<div class="ib-steps">', 1)[1]

    def step_form(self, panel, item):
        return panel.split(reverse("albright_planner:toggle", args=[item.pk]), 1)[1].split("</form>", 1)[0]

    def test_ideas_list_has_the_steps_form(self):
        panel = self.steps_panel()
        self.assertIn("No steps yet.", panel)
        self.assertIn('<button type="submit" class="ib-sbtn">Add step</button>', panel)
        self.assertIn('<input type="hidden" name="action" value="add_task">', panel)
        self.assertIn(f'name="date" value="{self.today.isoformat()}"', panel)
        self.assertIn('placeholder="Add a step…"', panel)
        self.assertNotIn('value="Dark charts"', panel)                  # no longer prefilled
        first, _second = self.two_tasks()
        form = self.step_form(self.steps_panel(), first)
        self.assertIn('aria-pressed="false">&#9744;</button>', form)
        self.assertIn(f"<span>{first.title}</span>", form)
        self.assertIn(f'name="date" value="{first.date.isoformat()}"', form)

    def test_check_a_step_from_the_list(self):
        first, second = self.two_tasks()
        back = "/control/ideas/"
        response = self.client.post(reverse("albright_planner:toggle", args=[first.pk]),
                                    {"date": first.date.isoformat(), "next": back})
        self.assertRedirects(response, back, fetch_redirect_response=False)
        self.assertTrue(ItemCompletion.objects.filter(item=first, occurrence_date=first.date).exists())
        panel = self.steps_panel()
        self.assertIn('aria-pressed="true">&#9745;</button>', self.step_form(panel, first))
        self.assertIn("text-decoration: line-through", self.step_form(panel, first))
        self.assertIn('aria-pressed="false">&#9744;</button>', self.step_form(panel, second))
        day = self.client.get(reverse("albright_planner:day") + f"?date={first.date.isoformat()}").content.decode()
        row = day.split(f'data-item="{first.pk}"', 1)[0].rsplit("<li", 1)[1]
        self.assertIn("is-done", row)

    def test_checked_in_the_planner_shows_checked_in_the_list(self):
        first, _second = self.two_tasks()
        self.client.post(reverse("albright_planner:toggle", args=[first.pk]), {"date": first.date.isoformat()})
        self.assertIn('aria-pressed="true">&#9745;</button>', self.step_form(self.steps_panel(), first))
        # ...and on the Idea page, which has the same toggle.
        page = self.client.get(self.page_url).content.decode()
        self.assertIn(reverse("albright_planner:toggle", args=[first.pk]), page)
        self.assertIn(f'aria-label="Mark not done: {first.title}"', page)

    def test_two_steps_listed_another_user_sees_none(self):
        first, second = self.two_tasks()
        panel = self.steps_panel()
        self.assertIn("Steps (2)", panel)
        for item in (first, second):
            self.assertIn(item.title, panel)
        client = Client()
        client.force_login(self.other)
        theirs = self.steps_panel(client)
        self.assertIn("No steps yet.", theirs)
        for item in (first, second):
            self.assertNotIn(item.title, theirs)

    def test_bad_task_form_shows_errors(self):
        response = self.client.post(self.page_url, {"action": "add_task", "title": "", "date": "not a date"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["task_form"].errors)
        self.assertFalse(ScheduleItem.objects.filter(idea=self.idea).exists())
