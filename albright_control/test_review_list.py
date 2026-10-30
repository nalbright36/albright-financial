"""Admin > Ideas and Admin > Bot suggestions as one shared list
(review_list.py, review_list_views.py, review_list.html, review_list.js):
the same statuses, status tabs that always open on New, ?status=, tab
counts, quick status changes and Undo (JSON and plain form posts), review
mode's queue, the empty state, the nav/overview New counts, the suggestion
status migration, and superuser-only access."""
import importlib
import json
import re
from datetime import timedelta

from django.apps import apps as django_apps
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import SiteSuggestion
from albright_control.models import AuditEntry, Idea
from albright_control.review_list import SHORTCUTS, STATUSES, TABS, ago
from albright_labs.testing import with_apps

PASSWORD = "pw-not-real-12345"
TEMPLATE = "albright_control/review_list.html"
PAGES = {"ideas": "albright_control:ideas", "suggestions": "albright_control:suggestions"}
STATUS_URLS = {"ideas": "albright_control:idea_status", "suggestions": "albright_control:suggestion_status"}


class ReviewListTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password=PASSWORD, email="")
        self.client.force_login(self.admin)
        now = timezone.now()
        self.items = {"ideas": {}, "suggestions": {}}
        for i, (title, status) in enumerate([("Oldest new", "new"), ("Middle new", "new"), ("Newest new", "new"),
                                             ("A plan", "planned"), ("Thinking", "considering"), ("Shipped", "done")]):
            idea = Idea.objects.create(title=f"Idea {title}", description=f"About {title}", original_message="m",
                                       status=status)
            suggestion = SiteSuggestion.objects.create(app="resell", title=f"Suggestion {title}",
                                                       description=f"About {title}", status=status)
            # Spread the created times so "oldest first" is well defined.
            Idea.objects.filter(pk=idea.pk).update(created_at=now - timedelta(hours=10 - i))
            SiteSuggestion.objects.filter(pk=suggestion.pk).update(created_at=now - timedelta(hours=10 - i))
            self.items["ideas"][title] = idea
            self.items["suggestions"][title] = suggestion

    def page(self, kind, **params):
        return self.client.get(reverse(PAGES[kind]), params)

    def titles(self, kind, **params):
        return [r["title"] for r in self.page(kind, **params).context["rows"]]

    def set_status(self, kind, obj, status, **extra):
        return self.client.post(reverse(STATUS_URLS[kind], args=[obj.pk]),
                                data=json.dumps({"status": status, **extra}), content_type="application/json",
                                HTTP_X_REQUESTED_WITH="XMLHttpRequest")


class TabsTests(ReviewListTestCase):
    def test_both_pages_use_the_shared_template(self):
        for kind in PAGES:
            with self.subTest(kind=kind):
                response = self.page(kind)
                self.assertTemplateUsed(response, TEMPLATE)
                self.assertContains(response, 'id="rl-tabs"')
                self.assertContains(response, "js/review_list.js")

    def test_opening_the_page_always_shows_new(self):
        for kind in PAGES:
            with self.subTest(kind=kind):
                self.page(kind, status="planned")            # look at another tab first...
                self.page(kind, status="all")
                response = self.page(kind)                   # ...then open the page plainly
                self.assertEqual(response.context["status"], "new")
                self.assertEqual(sorted(r["status"] for r in response.context["rows"]), ["new"] * 3)
                self.assertContains(response, 'class="scanner-tab is-active" data-tab="new" aria-current="page"')

    def test_status_param_picks_the_tab(self):
        prefix = {"ideas": "Idea", "suggestions": "Suggestion"}
        for kind in PAGES:
            with self.subTest(kind=kind):
                self.assertEqual(self.titles(kind, status="planned"), [f"{prefix[kind]} A plan"])
                self.assertEqual(self.titles(kind, status="considering"), [f"{prefix[kind]} Thinking"])
                self.assertEqual(len(self.titles(kind, status="all")), 6)
                self.assertEqual(len(self.titles(kind, status="bogus")), 3)        # unknown -> New
                self.assertEqual(self.page(kind, status="bogus").context["status"], "new")

    def test_tab_counts(self):
        for kind in PAGES:
            with self.subTest(kind=kind):
                response = self.page(kind)
                counts = {tab["key"]: tab["count"] for tab in response.context["tabs"]}
                self.assertEqual(counts, {"new": 3, "considering": 1, "planned": 1, "in_progress": 0, "done": 1,
                                          "declined": 0, "all": 6})
                self.assertEqual([tab["key"] for tab in response.context["tabs"]], [k for k, _l in TABS])
                self.assertContains(response, '<span class="tab-badge" data-count="new">3</span>')
                self.assertContains(response, '<span class="tab-badge" data-count="all">6</span>')

    def test_tabs_keep_the_filters(self):
        response = self.page("ideas", q="Plan", app="all")
        planned = next(tab for tab in response.context["tabs"] if tab["key"] == "planned")
        self.assertIn("q=Plan", planned["url"])
        self.assertIn("status=planned", planned["url"])
        self.assertEqual(self.titles("ideas", status="planned", q="plan"), ["Idea A plan"])
        self.assertEqual(self.titles("ideas", q="plan"), [])                     # filters apply within the tab

    def test_rows_show_the_summary_and_the_details(self):
        idea = self.items["ideas"]["Newest new"]
        Idea.objects.filter(pk=idea.pk).update(impact="high", effort="small", priority="urgent", source="grok",
                                               links=["https://example.com/spec"], my_notes="My take",
                                               tags=["ui"], description="Intro\n- first\n- second <b>")
        response = self.page("ideas")
        content = response.content.decode()
        row = content.split(f'id="idea-{idea.pk}"', 1)[1].split("</article>", 1)[0]
        self.assertIn('<span class="rl-row__title">Idea Newest new</span>', row)
        self.assertIn('<span class="rl-row__time" title="', row)
        self.assertIn("8h ago", row)
        badges = row.split('<span class="rl-row__badges">', 1)[1].split('<span class="rl-row__preview">', 1)[0]
        for badge in ("Whole site", "urgent priority", "high impact", "small effort", "Grok", "#ui"):
            self.assertIn(f">{badge}</span>", badges)
        self.assertIn('<span class="rl-row__preview">Intro\n- first\n- second &lt;b&gt;</span>', row)
        # Expanded: the description's "- " lines as a real list (escaped), the original message, links, notes.
        self.assertIn('<div class="rl-row__text"><p>Intro</p><ul><li>first</li><li>second &lt;b&gt;</li></ul></div>', row)
        self.assertIn('<pre class="crypto-source-text">m</pre>', row)
        self.assertIn("https://example.com/spec", row)
        self.assertIn(">My take</textarea>", row)
        for action in ("Copy as Claude Code prompt", "Merge duplicates", ">Delete</button>"):
            self.assertIn(action, row)

        suggestion = self.items["suggestions"]["Newest new"]
        row = self.page("suggestions").content.decode().split(f'id="suggestion-{suggestion.pk}"', 1)[1].split("</article>", 1)[0]
        self.assertIn("Move to Ideas", row)
        self.assertIn(reverse("albright_control:suggestion_delete", args=[suggestion.pk]), row)
        self.assertNotIn("Merge duplicates", row)

    def test_row_structure_on_both_pages(self):
        """Card row: one reset <button> holding title + time, the badges and the preview; then the status buttons."""
        for kind in PAGES:
            with self.subTest(kind=kind):
                obj = self.items[kind]["Middle new"]
                content = self.page(kind).content.decode()
                row = content.split(f'data-id="{obj.pk}"', 1)[1].split("</article>", 1)[0]
                toggle = row.split('<button type="button" class="rl-row__toggle"', 1)[1].split("</button>", 1)[0]
                self.assertIn(f'aria-controls="rl-body-{obj.pk}"', toggle)
                self.assertLess(toggle.index('class="rl-row__line"'), toggle.index('class="rl-row__badges"'))
                self.assertLess(toggle.index('class="rl-row__badges"'), toggle.index('class="rl-row__preview"'))
                self.assertIn(f'<span class="rl-row__title">{obj.title}</span>', toggle)
                self.assertIn(f'<div class="rl-row__body" id="rl-body-{obj.pk}" hidden>', row)
                self.assertIn('class="card rl-row"', content)
                self.assertIn('class="scanner-controls rl-filters"', content)
                self.assertIn('id="rl-filters-submit"', content)          # no-JS fallback (hidden by the JS)

    def test_status_buttons_exclude_the_current_status_and_no_menu(self):
        for kind in PAGES:
            for name, status in (("Middle new", "new"), ("A plan", "planned"), ("Shipped", "done")):
                with self.subTest(kind=kind, status=status):
                    obj = self.items[kind][name]
                    content = self.page(kind, status=status).content.decode()
                    row = content.split(f'data-id="{obj.pk}"', 1)[1].split("</article>", 1)[0]
                    form = row.split("data-status-form", 1)[1].split("</form>", 1)[0]
                    shown = [label for _value, label in
                             re.findall(r'value="(\w+)" class="btn btn-secondary btn-sm[^"]*">([^<]+)</button>', form)]
                    expected = [label for key, label in STATUSES if key not in ("new", status)]
                    self.assertEqual(shown, expected)
                    self.assertNotIn('value="new"', form)
                    self.assertIn('rl-status--declined', form)
                    self.assertNotIn("rl-more", content)
                    self.assertNotIn("&middot;&middot;&middot;", content)
                    self.assertNotIn("<details class=\"rl-more", content)

    def test_empty_new_tab(self):
        Idea.objects.filter(status="new").update(status="planned")
        response = self.page("ideas")
        self.assertContains(response, "All caught up - nothing new to review.")
        self.assertContains(response, f'href="?status=planned">Planned</a>')
        self.assertNotContains(response, 'id="rl-start-review"')
        self.assertNotContains(response, 'id="rl-queue"')
        filtered = self.page("ideas", status="done", q="nothing like this")
        self.assertContains(filtered, "Nothing in Done matches these filters.")


class StatusChangeTests(ReviewListTestCase):
    def test_quick_status_updates_counts_and_leaves_new(self):
        for kind in PAGES:
            with self.subTest(kind=kind):
                obj = self.items[kind]["Middle new"]
                response = self.set_status(kind, obj, "planned")
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertEqual((data["status"], data["label"], data["previous"]), ("planned", "Planned", "new"))
                self.assertEqual((data["counts"]["new"], data["counts"]["planned"], data["counts"]["all"]), (2, 2, 6))
                self.assertEqual(data["new_counts"][kind], 2)
                obj.refresh_from_db()
                self.assertEqual(obj.status, "planned")
                self.assertNotIn(obj.title, self.titles(kind))
                self.assertIn(obj.title, self.titles(kind, status="planned"))

    def test_undo_puts_it_back(self):
        for kind in PAGES:
            with self.subTest(kind=kind):
                obj = self.items[kind]["Oldest new"]
                self.set_status(kind, obj, "declined")
                data = self.set_status(kind, obj, "new", undo=True).json()
                self.assertEqual((data["status"], data["counts"]["new"], data["counts"]["declined"]), ("new", 3, 0))
                self.assertIn(obj.title, self.titles(kind))
        details = list(AuditEntry.objects.order_by("pk").values_list("action", "details"))
        self.assertEqual([a for a, _d in details], ["idea_status", "idea_status", "suggestion_status", "suggestion_status"])
        self.assertTrue(details[0][1].endswith("new -> declined"))
        self.assertTrue(details[1][1].endswith("declined -> new (undo)"))

    def test_idea_status_changed_at_follows(self):
        idea = self.items["ideas"]["Oldest new"]
        before = idea.status_changed_at
        self.set_status("ideas", idea, "in_progress")
        idea.refresh_from_db()
        self.assertGreater(idea.status_changed_at, before)

    def test_unknown_status_is_refused(self):
        obj = self.items["suggestions"]["Oldest new"]
        self.assertEqual(self.set_status("suggestions", obj, "accepted").status_code, 400)
        obj.refresh_from_db()
        self.assertEqual(obj.status, "new")

    def test_plain_form_post_goes_back(self):
        obj = self.items["ideas"]["Oldest new"]
        back = reverse("albright_control:ideas") + "?status=new&q=Idea"
        response = self.client.post(reverse("albright_control:idea_status", args=[obj.pk]),
                                    {"status": "considering", "next": back})
        self.assertRedirects(response, back, fetch_redirect_response=False)
        response = self.client.post(reverse("albright_control:idea_status", args=[obj.pk]),
                                    {"status": "planned", "next": "https://evil.example/"})
        self.assertRedirects(response, reverse("albright_control:ideas"), fetch_redirect_response=False)
        obj.refresh_from_db()
        self.assertEqual(obj.status, "planned")

    def test_notes_save(self):
        for kind, url in (("ideas", "albright_control:idea_notes"), ("suggestions", "albright_control:suggestion_notes")):
            with self.subTest(kind=kind):
                obj = self.items[kind]["Oldest new"]
                response = self.client.post(reverse(url, args=[obj.pk]), data=json.dumps({"my_notes": "Worth it"}),
                                            content_type="application/json")
                self.assertEqual(response.json(), {"ok": True, "id": obj.pk, "notes": "Worth it"})
                obj.refresh_from_db()
                self.assertEqual(obj.my_notes, "Worth it")
                too_long = self.client.post(reverse(url, args=[obj.pk]), data=json.dumps({"my_notes": "x" * 5001}),
                                            content_type="application/json")
                self.assertEqual(too_long.status_code, 400)

    def test_delete_suggestion(self):
        obj = self.items["suggestions"]["Oldest new"]
        response = self.client.post(reverse("albright_control:suggestion_delete", args=[obj.pk]))
        self.assertRedirects(response, reverse("albright_control:suggestions"), fetch_redirect_response=False)
        self.assertFalse(SiteSuggestion.objects.filter(pk=obj.pk).exists())
        self.assertEqual(AuditEntry.objects.get().action, "suggestion_deleted")


class ReviewModeTests(ReviewListTestCase):
    def queue(self, kind, **params):
        content = self.page(kind, **params).content.decode()
        return json.loads(content.split('<script id="rl-queue" type="application/json">', 1)[1].split("</script>", 1)[0])

    def test_queue_is_new_items_oldest_first(self):
        for kind in PAGES:
            with self.subTest(kind=kind):
                queue = self.queue(kind)
                self.assertEqual([item["title"].split(" ", 1)[1] for item in queue], ["Oldest new", "Middle new", "Newest new"])
                self.assertEqual(queue[0]["urls"]["status"], reverse(STATUS_URLS[kind], args=[queue[0]["id"]]))
                self.assertIn("App:", queue[0]["prompt"])
                self.assertContains(self.page(kind), 'id="rl-start-review">Start review</button>')

    def test_only_on_the_new_tab(self):
        response = self.page("ideas", status="planned")
        self.assertNotContains(response, 'id="rl-queue"')
        self.assertNotContains(response, "Start review")

    def test_legend_lists_every_key(self):
        content = self.page("ideas").content.decode()
        legend = content.split('id="rv-legend"', 1)[1].split("</aside>", 1)[0]
        self.assertEqual([action for _keys, _label, action in SHORTCUTS],
                         ["considering", "planned", "in_progress", "declined", "note", "copy", "skip", "back", "undo", "help"])
        for keys, label, action in SHORTCUTS:
            self.assertIn(f'data-legend="{action}"', legend)
            self.assertIn(label, legend)
            for key in keys:
                self.assertIn(f'<kbd class="kbd">{key}</kbd>', legend)
        self.assertIn('class="review-layout"', content)          # the reseller review queue's layout and card
        self.assertIn('class="card review-card"', content)

    def test_review_actions_with_undo_update_new(self):
        """What review mode does for C / P / X and then U: the same status endpoint (undo flagged)."""
        queue = self.queue("suggestions")
        first, second = (SiteSuggestion.objects.get(pk=item["id"]) for item in queue[:2])
        self.assertEqual(self.set_status("suggestions", first, "considering").json()["counts"]["new"], 2)
        self.assertEqual(self.set_status("suggestions", second, "declined").json()["counts"]["new"], 1)
        self.assertEqual(len(self.queue("suggestions")), 1)
        undone = self.set_status("suggestions", second, "new", undo=True).json()
        self.assertEqual((undone["counts"]["new"], undone["new_counts"]["suggestions"]), (2, 2))
        self.assertEqual([item["id"] for item in self.queue("suggestions")], [second.pk, queue[2]["id"]])


class NavAndOverviewTests(ReviewListTestCase):
    def test_nav_badges_count_new(self):
        content = self.page("ideas").content.decode()
        bot = content.split('data-nav-group="bot"', 1)[1].split('data-nav-group="system"', 1)[0]
        self.assertIn('<span class="badge warn navbar__badge">6<span class="visually-hidden"> need attention</span>', bot)
        self.assertIn('<span>Ideas</span> <span class="badge warn navbar__badge">3</span>', bot)
        self.assertIn('<span>Bot suggestions</span> <span class="badge warn navbar__badge">3</span>', bot)

    def test_overview_tiles_link_to_new(self):
        response = self.client.get(reverse("albright_control:overview"))
        self.assertContains(response, f'href="{reverse("albright_control:ideas")}?status=new"')
        self.assertContains(response, f'href="{reverse("albright_control:suggestions")}?status=new"')
        tile = response.content.decode().split('id="suggestion-counts"', 1)[1].split("</a>", 1)[0]
        self.assertIn("3 new", tile)
        self.assertIn("1 planned &middot; 0 in progress", tile)


class AccessTests(ReviewListTestCase):
    def test_superusers_only(self):
        regular = with_apps(User.objects.create_user(username="regular", password=PASSWORD))
        staff = with_apps(User.objects.create_user(username="staffer", password=PASSWORD, is_staff=True))
        idea, suggestion = self.items["ideas"]["Oldest new"], self.items["suggestions"]["Oldest new"]
        urls = [reverse("albright_control:ideas"), reverse("albright_control:suggestions"),
                reverse("albright_control:idea_status", args=[idea.pk]),
                reverse("albright_control:idea_notes", args=[idea.pk]),
                reverse("albright_control:suggestion_status", args=[suggestion.pk]),
                reverse("albright_control:suggestion_notes", args=[suggestion.pk]),
                reverse("albright_control:suggestion_delete", args=[suggestion.pk])]
        for who in (None, regular, staff):
            client = Client()
            if who:
                client.force_login(who)
            for url in urls:
                with self.subTest(who=who and who.username, url=url):
                    self.assertEqual(client.get(url).status_code, 404)
                    self.assertEqual(client.post(url, {"status": "done", "my_notes": "x"}).status_code, 404)
        idea.refresh_from_db()
        suggestion.refresh_from_db()
        self.assertEqual((idea.status, suggestion.status, idea.my_notes), ("new", "new", ""))
        self.assertTrue(SiteSuggestion.objects.filter(pk=suggestion.pk).exists())


class StatusesTests(TestCase):
    def test_both_pages_share_the_statuses(self):
        self.assertEqual(SiteSuggestion.STATUSES, Idea.STATUSES)
        self.assertEqual([k for k, _l in STATUSES], ["new", "considering", "planned", "in_progress", "done", "declined"])

    def test_suggestion_status_migration(self):
        migration = importlib.import_module("albright_bot_api.migrations.0004_sitesuggestion_idea_statuses")
        rows = {}
        for old in ("new", "accepted", "declined", "done"):
            s = SiteSuggestion.objects.create(app="all", title=old, description="d")
            SiteSuggestion.objects.filter(pk=s.pk).update(status=old)      # the old values, as stored before
            rows[old] = s.pk
        migration.remap(migration.FORWARD)(django_apps, None)
        after = dict(SiteSuggestion.objects.values_list("pk", "status"))
        self.assertEqual({old: after[pk] for old, pk in rows.items()},
                         {"new": "new", "accepted": "planned", "declined": "declined", "done": "done"})
        extra = SiteSuggestion.objects.create(app="all", title="x", description="d", status="considering")
        migration.remap(migration.BACKWARD)(django_apps, None)
        back = dict(SiteSuggestion.objects.values_list("pk", "status"))
        self.assertEqual({old: back[pk] for old, pk in rows.items()},
                         {"new": "new", "accepted": "accepted", "declined": "declined", "done": "done"})
        self.assertEqual(back[extra.pk], "accepted")

    def test_ago(self):
        now = timezone.now()
        self.assertEqual(ago(now - timedelta(seconds=20), now), "just now")
        self.assertEqual(ago(now - timedelta(minutes=5), now), "5m ago")
        self.assertEqual(ago(now - timedelta(hours=2, minutes=59), now), "2h ago")
        self.assertEqual(ago(now - timedelta(days=3), now), "3d ago")
        self.assertRegex(ago(now - timedelta(days=60), now), r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$")
