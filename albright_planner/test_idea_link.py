"""Planner tasks for an Admin Idea: one main task (ScheduleItem.idea, parent empty - titled like the Idea) and its
subtasks (parent = the main task), each its own Planner item and date. Listed on the Idea page and in the Ideas
reviewer's Planner tab with done/open for that occurrence - the same check-off as the Planner, both ways - only ever
the viewer's own; unlinked when the Idea is deleted; ↳ and links back on the Planner side; and the migration that
turned older Idea tasks into a main task and subtasks."""
import importlib
from datetime import timedelta

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
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

    def add_main(self, day=None, client=None, **extra):
        return (client or self.client).post(self.page_url, {"action": "add_main",
                                                            "date": (day or self.today).isoformat(), **extra})

    def add_sub(self, title, day, client=None, **extra):
        return (client or self.client).post(self.page_url, {"action": "add_subtask", "title": title,
                                                            "date": day.isoformat(), **extra})

    def main_and_sub(self):
        self.add_main(notes="the plan")
        self.add_sub("Sketch the dark palette", self.today + timedelta(days=3))
        main = ScheduleItem.objects.get(idea=self.idea, parent__isnull=True, owner=self.me)
        return main, ScheduleItem.objects.get(idea=self.idea, parent=main)

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    # Main task and subtasks ------------------------------------------------------
    def test_main_then_subtask(self):
        response = self.add_main()
        self.assertRedirects(response, self.page_url, fetch_redirect_response=False)
        self.add_sub("Sketch the dark palette", self.today + timedelta(days=3))
        main = ScheduleItem.objects.get(idea=self.idea, parent__isnull=True)
        sub = ScheduleItem.objects.get(idea=self.idea, parent=main)
        self.assertEqual((main.title, main.date), ("Dark charts", self.today))
        self.assertEqual((sub.title, sub.date), ("Sketch the dark palette", self.today + timedelta(days=3)))
        for item in (main, sub):
            self.assertEqual((item.owner, item.kind, item.idea_id, item.source_url, item.source_app, item.source_id),
                             (self.me, ScheduleItem.TASK, self.idea.pk, f"/control/ideas/{self.idea.pk}/", "", ""))
            self.assertFalse(item.locked)
        self.assertEqual(list(main.subtasks.all()), [sub])

    def test_add_main_twice_makes_one(self):
        self.add_main()
        response = self.add_main(day=self.today + timedelta(days=1))
        self.assertEqual(ScheduleItem.objects.filter(idea=self.idea).count(), 1)
        self.assertIn("Already in your Planner.", self.messages(response))
        # The older form's action name does the same.
        self.client.post(self.page_url, {"action": "add_task", "title": "x", "date": self.today.isoformat()})
        self.assertEqual(ScheduleItem.objects.filter(idea=self.idea).count(), 1)

    def test_add_task_is_an_alias_for_add_main(self):
        self.client.post(self.page_url, {"action": "add_task", "title": "ignored", "date": self.today.isoformat()})
        main = ScheduleItem.objects.get(idea=self.idea)
        self.assertEqual((main.title, main.parent_id), ("Dark charts", None))

    def test_first_subtask_adds_the_main_task_too(self):
        self.add_sub("First step", self.today + timedelta(days=2))
        items = ScheduleItem.objects.filter(idea=self.idea)
        self.assertEqual(items.count(), 2)
        main = items.get(parent__isnull=True)
        sub = items.get(parent__isnull=False)
        self.assertEqual((main.title, main.date), ("Dark charts", self.today + timedelta(days=2)))
        self.assertEqual((sub.title, sub.parent_id), ("First step", main.pk))

    def test_form_defaults_to_today(self):
        response = self.client.get(self.page_url)
        self.assertEqual(response.context["task_form"].initial["date"], self.today)
        self.assertContains(response, ">Add idea to Planner</button>")
        self.assertNotContains(response, ">Add subtask</button>")
        self.add_main()
        response = self.client.get(self.page_url)
        self.assertContains(response, ">Add subtask</button>")
        self.assertNotContains(response, ">Add idea to Planner</button>")

    def test_idea_page_lists_main_and_subtasks_with_done(self):
        main, sub = self.main_and_sub()
        ItemCompletion.objects.create(item=main, occurrence_date=main.date, completed_by=self.me)
        response = self.client.get(self.page_url)
        block = response.context["planner"]
        self.assertEqual((block["main"]["item"], block["main"]["done"], block["done"], block["total"]), (main, True, 1, 2))
        self.assertEqual([(r["item"], r["done"]) for r in block["subs"]], [(sub, False)])
        section = response.content.decode().split('id="idea-planner-tasks"', 1)[1].split("</section>", 1)[0]
        for item, label in ((main, "Done"), (sub, "Open")):
            row = section.split(f'data-item="{item.pk}"', 1)[1].split("</li>", 1)[0]
            self.assertIn(f">{label}</span>", row)
            self.assertIn(item.title, row)
            self.assertIn(reverse("albright_planner:item", args=[item.pk]), row)
            self.assertIn(reverse("albright_planner:toggle", args=[item.pk]), row)
        self.assertIn("&#8627;", section.split(f'data-item="{sub.pk}"', 1)[1].split("</li>", 1)[0])

    def test_archived_subtasks_are_left_out(self):
        main, sub = self.main_and_sub()
        ScheduleItem.objects.filter(pk=sub.pk).update(archived=True)
        block = self.client.get(self.page_url).context["planner"]
        self.assertEqual((block["main"]["item"], block["subs"], block["total"]), (main, [], 1))

    def test_another_user_never_sees_my_idea_tasks(self):
        main, sub = self.main_and_sub()
        client = Client()
        client.force_login(self.other)
        response = client.get(self.page_url)
        self.assertIsNone(response.context["planner"]["main"])
        self.assertNotContains(response, "Sketch the dark palette")
        self.assertFalse(ScheduleItem.objects.visible_to(self.other).filter(pk__in=[main.pk, sub.pk]).exists())
        # They get their own main task for the same Idea, seen only by them.
        self.add_main(client=client)
        self.add_sub("Their take", self.today, client=client)
        self.assertNotContains(self.client.get(self.page_url), "Their take")
        self.assertContains(client.get(self.page_url), "Their take")
        self.assertEqual(self.client.get(self.page_url).context["planner"]["main"]["item"], main)

    def test_deleting_the_idea_unlinks_the_tasks(self):
        main, sub = self.main_and_sub()
        self.client.post(self.page_url, {"action": "delete"})
        self.assertFalse(Idea.objects.filter(pk=self.idea.pk).exists())
        for item in (main, sub):
            item.refresh_from_db()
            self.assertEqual((item.idea_id, item.source_url), (None, ""))

    def test_deleting_the_main_task_keeps_the_idea_and_frees_the_subtask(self):
        main, sub = self.main_and_sub()
        main.delete()
        sub.refresh_from_db()
        self.assertTrue(Idea.objects.filter(pk=self.idea.pk).exists())
        self.assertEqual((sub.parent_id, sub.idea_id), (None, self.idea.pk))

    def test_next_goes_back_to_the_list_offsite_does_not(self):
        back = "/control/ideas/?status=new"
        self.assertRedirects(self.add_main(next=back), back, fetch_redirect_response=False)
        self.assertRedirects(self.add_sub("From the list", self.today, next=back), back, fetch_redirect_response=False)
        response = self.add_sub("Elsewhere", self.today, next="https://evil.example/")
        self.assertRedirects(response, self.page_url, fetch_redirect_response=False)
        self.assertEqual(ScheduleItem.objects.filter(idea=self.idea, parent__isnull=False).count(), 2)

    def test_bad_forms_show_errors(self):
        response = self.client.post(self.page_url, {"action": "add_main", "date": "not a date"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["task_form"].errors)
        self.add_main()
        response = self.client.post(self.page_url, {"action": "add_subtask", "title": "", "date": "nope"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["sub_form"].errors)
        self.assertEqual(ScheduleItem.objects.filter(idea=self.idea).count(), 1)

    # The Ideas reviewer's tabs ---------------------------------------------------
    def detail(self, client=None):
        content = (client or self.client).get(reverse("albright_control:ideas")).content.decode()
        return content.split(f'id="rl-detail-{self.idea.pk}"', 1)[1].split("</section>", 1)[0]

    def pane(self, name, client=None):
        return self.detail(client).split(f'data-dpane="{name}"', 1)[1]

    def item_form(self, pane, item):
        return pane.split(f'data-plan-item="{item.pk}"', 1)[1].split("</form>", 1)[0]

    def test_reviewer_tabs_and_footer(self):
        detail = self.detail()
        for tab in ("details", "planner", "notes"):
            self.assertIn(f'data-dtab="{tab}"', detail)
        self.assertIn('class="ib-tabs ib-dtabs"', detail)
        self.assertIn('<div data-dpane="planner" class="ib-plan" hidden>', detail)
        self.assertIn('<div data-dpane="notes" hidden>', detail)
        foot = detail.split('class="ib-foot"', 1)[1]
        for part in (">Edit</a>", ">Merge</button>", ">Delete</button>", 'style="margin-left: auto;"'):
            self.assertIn(part, foot)
        self.assertNotIn("Add a step", detail)                          # the old Steps block is gone

    def test_reviewer_add_idea_to_planner_then_subtasks(self):
        planner = self.pane("planner")
        self.assertIn('<button type="submit" class="ib-sbtn primary">Add idea to Planner</button>', planner)
        self.assertIn('<input type="hidden" name="action" value="add_main">', planner)
        self.assertIn(f'name="date" value="{self.today.isoformat()}"', planner)
        self.add_main()
        self.assertIn('Planner<span class="ib-count">0/1</span>', self.detail())
        self.add_sub("Sketch the dark palette", self.today + timedelta(days=3))
        main = ScheduleItem.objects.get(idea=self.idea, parent__isnull=True)
        sub = ScheduleItem.objects.get(idea=self.idea, parent=main)
        planner = self.pane("planner")
        main_form = self.item_form(planner, main)
        self.assertIn(f"<strong>{main.title}</strong>", main_form)
        self.assertIn("Open in Planner &#8599;", main_form)
        sub_form = self.item_form(planner, sub)
        self.assertIn('<span aria-hidden="true">&#8627;</span>', sub_form)
        self.assertIn('aria-pressed="false"', sub_form)
        self.assertIn(f"<span>{sub.title}</span>", sub_form)
        self.assertIn('placeholder="Add a subtask…"', planner)
        self.assertIn('<input type="hidden" name="action" value="add_subtask">', planner)
        self.assertNotIn("Add idea to Planner", planner)
        self.assertLess(planner.index(f'data-plan-item="{sub.pk}"'), planner.index('class="ib-row ib-sub ib-add"'))
        self.assertIn('Planner<span class="ib-count">0/2</span>', self.detail())

    def test_toggle_from_the_planner_shows_in_the_reviewer(self):
        main, sub = self.main_and_sub()
        self.client.post(reverse("albright_planner:toggle", args=[sub.pk]), {"date": sub.date.isoformat()})
        sub_form = self.item_form(self.pane("planner"), sub)
        self.assertIn('aria-pressed="true"', sub_form)
        self.assertIn('class="ib-done"', sub_form)
        self.assertIn('Planner<span class="ib-count">1/2</span>', self.detail())

    def test_toggle_from_the_reviewer_shows_in_the_planner(self):
        main, sub = self.main_and_sub()
        back = "/control/ideas/"
        response = self.client.post(reverse("albright_planner:toggle", args=[sub.pk]),
                                    {"date": sub.date.isoformat(), "next": back})
        self.assertRedirects(response, back, fetch_redirect_response=False)
        self.assertTrue(ItemCompletion.objects.filter(item=sub, occurrence_date=sub.date).exists())
        day = self.client.get(reverse("albright_planner:day") + f"?date={sub.date.isoformat()}").content.decode()
        self.assertIn("is-done", day.split(f'data-item="{sub.pk}"', 1)[0].rsplit("<li", 1)[1])

    def test_reviewer_shows_another_user_none_of_it(self):
        self.main_and_sub()
        client = Client()
        client.force_login(self.other)
        detail = self.detail(client)
        self.assertNotIn("Sketch the dark palette", detail)
        self.assertNotIn('class="ib-count"', detail.split('class="ib-tabs ib-dtabs"', 1)[1].split("</nav>", 1)[0])
        self.assertIn("Add idea to Planner", detail)

    # The Planner side -------------------------------------------------------------
    def test_planner_rows_show_subtask_mark_and_idea_gear(self):
        main, sub = self.main_and_sub()
        day = self.client.get(reverse("albright_planner:day") + f"?date={sub.date.isoformat()}").content.decode()
        row = day.split(f'data-item="{sub.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertIn('<span class="pl-subtask" title="Subtask" aria-label="Subtask">&#8627;</span>', row)
        self.assertIn(f'<a href="/control/ideas/{self.idea.pk}/" class="pl-src pl-idea" title="Open its Idea"', row)
        self.assertIn(reverse("albright_planner:toggle", args=[sub.pk]), row)     # not locked: still checkable
        main_day = self.client.get(reverse("albright_planner:day") + f"?date={main.date.isoformat()}").content.decode()
        self.assertNotIn("pl-subtask", main_day.split(f'data-item="{main.pk}"', 1)[1].split("</li>", 1)[0])

    def test_view_panel_links_parent_idea_and_subtasks(self):
        main, sub = self.main_and_sub()
        panel = self.client.get(reverse("albright_planner:item", args=[sub.pk]) + "?panel=1").content.decode()
        self.assertIn(f'Subtask of <a href="{reverse("albright_planner:item", args=[main.pk])}" data-view>{main.title}</a>',
                      panel)
        self.assertIn(f'href="{self.page_url}" class="pl-view__fact" id="pl-view-idea">Idea #{self.idea.pk}', panel)
        panel = self.client.get(reverse("albright_planner:item", args=[main.pk]) + "?panel=1").content.decode()
        self.assertIn('id="pl-view-subtasks"', panel)
        self.assertIn(f'href="{reverse("albright_planner:item", args=[sub.pk])}" data-view>{sub.title}</a>', panel)
        self.assertNotIn("Subtask of", panel)


class SubtaskMigrationTests(TestCase):
    def test_older_idea_tasks_become_main_and_subtasks(self):
        owner = User.objects.create_user(username="planner-owner", password=PASSWORD)
        someone = User.objects.create_user(username="someone-else", password=PASSWORD)
        idea = Idea.objects.create(title="Old idea", description="d", original_message="m")
        day = timezone.localdate()
        first = ScheduleItem.objects.create(owner=owner, title="First", date=day + timedelta(days=5), idea=idea)
        later = ScheduleItem.objects.create(owner=owner, title="Later", date=day, idea=idea)
        theirs = ScheduleItem.objects.create(owner=someone, title="Theirs", date=day, idea=idea)
        plain = ScheduleItem.objects.create(owner=owner, title="No idea", date=day)
        migration = importlib.import_module("albright_planner.migrations.0016_scheduleitem_parent")
        migration.link_subtasks(django_apps, None)
        parents = dict(ScheduleItem.objects.filter(pk__in=[first.pk, later.pk, theirs.pk, plain.pk])
                       .values_list("pk", "parent_id"))
        self.assertEqual(parents, {first.pk: None, later.pk: first.pk, theirs.pk: None, plain.pk: None})
