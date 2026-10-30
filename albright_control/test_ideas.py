"""Admin > Ideas: the ideas:write / ideas:read scopes and endpoints
(required original_message, defaults for invalid choices, status "new" and
source "grok" always, duplicate marking, pagination), the Ideas page
(filters, sorting, Quick wins), merge, move from Bot suggestions, the
Claude Code prompt, overview counts, and the audit log."""
import json

from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from albright_bot_api.models import SCOPES, BotToken, SiteSuggestion
from albright_control.ideas import claude_code_prompt, find_duplicate, similarity
from albright_control.models import AuditEntry, Idea
from albright_control.prompts import CLOSING
from albright_labs.testing import with_apps

PASSWORD = "pw-not-real-12345"
MESSAGE = "hey grok - I want the watchlist to beep when a lot I'm watching drops under my max in the last 10 min"
GOOD = {"original_message": MESSAGE, "title": "Watchlist sound alert under max", "description": "Play a sound.",
        "app": "resell", "category": "feature", "priority": "high", "impact": "high", "effort": "small",
        "tags": ["watchlist", "alerts", "watchlist"], "links": ["https://example.com/spec", "javascript:alert(1)"],
        "target_date": "2026-12-01"}


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class IdeasApiTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password=PASSWORD, email="")
        self.token, self.raw = BotToken.issue(self.admin, "Grok bot", ["ideas:write", "ideas:read"])
        self.api = Client()

    def post(self, body, raw=None):
        return self.api.post(reverse("bot_api_v1:ideas"), data=json.dumps(body), content_type="application/json",
                             HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}")

    def get(self, raw=None, **params):
        return self.api.get(reverse("bot_api_v1:ideas"), params, HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}")

    def test_scopes_enforced(self):
        _t, none_raw = BotToken.issue(self.admin, "No ideas", ["site:suggest"])
        self.assertEqual(self.post(GOOD, raw=none_raw).status_code, 403)
        self.assertEqual(self.get(raw=none_raw).status_code, 403)
        _t, read_raw = BotToken.issue(self.admin, "Reader", ["ideas:read"])
        self.assertEqual(self.post(GOOD, raw=read_raw).status_code, 403)
        self.assertEqual(self.get(raw=read_raw).status_code, 200)
        _t, write_raw = BotToken.issue(self.admin, "Writer", ["ideas:write"])
        self.assertEqual(self.post(GOOD, raw=write_raw).status_code, 201)
        self.assertEqual(self.get(raw=write_raw).status_code, 403)
        self.assertEqual(self.api.delete(reverse("bot_api_v1:ideas"), HTTP_AUTHORIZATION=f"Bearer {self.raw}").status_code, 405)

    def test_create(self):
        response = self.post(GOOD)
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["status"], body["possible_duplicate_of"], body["defaults_applied"]), ("new", None, []))
        idea = Idea.objects.get(pk=body["id"])
        self.assertEqual((idea.original_message, idea.title, idea.app, idea.category, idea.priority, idea.impact,
                          idea.effort), (MESSAGE, GOOD["title"], "resell", "feature", "high", "high", "small"))
        self.assertEqual(idea.tags, ["watchlist", "alerts"])
        self.assertEqual(idea.links, ["https://example.com/spec"])       # non-http links dropped
        self.assertEqual(idea.target_date.isoformat(), "2026-12-01")
        self.assertEqual((idea.source, idea.token, idea.status), ("grok", self.token, "new"))
        self.assertIsNotNone(idea.status_changed_at)
        entry = AuditEntry.objects.get()
        self.assertEqual(entry.action, "idea_created")
        self.assertIn("by Grok bot (Bot API)", entry.details)

    def test_original_message_required(self):
        for body in ({k: v for k, v in GOOD.items() if k != "original_message"}, {**GOOD, "original_message": "  "}):
            response = self.post(body)
            self.assertEqual(response.status_code, 400)
            self.assertIn("original_message", response.json()["error"])
        self.assertEqual(self.post({**GOOD, "title": ""}).status_code, 400)
        self.assertEqual(self.post({**GOOD, "description": "x" * 5001}).status_code, 400)
        self.assertEqual(self.post({**GOOD, "target_date": "next week"}).status_code, 400)
        self.assertFalse(Idea.objects.exists())

    def test_invalid_choices_fall_back_to_defaults(self):
        body = self.post({"original_message": "x", "title": "Thing", "app": "crypto", "category": "wish",
                          "priority": "asap", "impact": 9, "effort": "huge"}).json()
        idea = Idea.objects.get(pk=body["id"])
        self.assertEqual((idea.app, idea.category, idea.priority, idea.impact, idea.effort),
                         ("all", "feature", "medium", "medium", "medium"))
        self.assertEqual(sorted(body["defaults_applied"]), ["app", "category", "effort", "impact", "priority"])
        omitted = Idea.objects.get(pk=self.post({"original_message": "y", "title": "Other"}).json()["id"])
        self.assertEqual((omitted.app, omitted.priority), ("all", "medium"))

    def test_status_notes_and_source_cant_be_set_by_the_bot(self):
        body = self.post({**GOOD, "status": "done", "my_notes": "approved!", "source": "manual"}).json()
        idea = Idea.objects.get(pk=body["id"])
        self.assertEqual((idea.status, idea.my_notes, idea.source), ("new", "", "grok"))

    def test_possible_duplicates_are_saved_and_marked(self):
        first = self.post(GOOD).json()["id"]
        second = self.post({**GOOD, "title": "watchlist SOUND alert, under max!"}).json()
        self.assertEqual(second["possible_duplicate_of"], first)
        self.assertEqual(Idea.objects.count(), 2)
        self.assertEqual(Idea.objects.get(pk=second["id"]).possible_duplicate_of_id, first)
        different = self.post({**GOOD, "title": "Calendar for opportunity deadlines"}).json()
        self.assertIsNone(different["possible_duplicate_of"])
        Idea.objects.filter(pk__in=[first, second["id"]]).update(status="done")   # closed ideas aren't matched
        self.assertIsNone(self.post({**GOOD, "title": "Watchlist sound alert under max"}).json()["possible_duplicate_of"])
        self.assertGreater(similarity("Watchlist sound alert under max", "watchlist sound-alert under max"), 0.95)

    def test_list_newest_first_paginated_and_minimal(self):
        for i in range(55):
            Idea.objects.create(title=f"Idea {i}", original_message="m", my_notes="private notes")
        page1 = self.get().json()
        self.assertEqual((page1["page"], page1["pages"], page1["total"], page1["next_page"]), (1, 2, 55, 2))
        self.assertEqual(page1["ideas"][0]["title"], "Idea 54")
        self.assertEqual(set(page1["ideas"][0]), {"id", "title", "app", "status", "created_at"})
        page2 = self.get(page=2).json()
        self.assertEqual((len(page2["ideas"]), page2["next_page"]), (5, None))
        self.assertNotIn("private notes", json.dumps(page1) + json.dumps(page2))
        self.assertEqual(self.get(page="abc").status_code, 400)

    def test_docs_and_scope_descriptions(self):
        text = self.api.get(reverse("bot_api_v1:docs"), HTTP_AUTHORIZATION=f"Bearer {self.raw}").content.decode()
        self.assertIn("POST ideas", text)
        self.assertIn("GET  ideas?page=N", text)
        self.assertIn("original_message", text)
        scopes = dict(SCOPES)
        self.assertIn("ideas:write", scopes)
        self.assertIn("ideas:read", scopes)
        client = Client()
        client.force_login(self.admin)
        page = client.get(reverse("albright_control:bot_api")).content.decode()
        create_form = page.split('id="token-form"', 1)[1].split("</form>", 1)[0]
        edit_form = page.split(f'id="edit-token-{self.token.pk}"', 1)[1].split("</details>", 1)[0]
        for form in (create_form, edit_form):
            self.assertIn('value="ideas:write"', form)
            self.assertIn('value="ideas:read"', form)
        self.assertIn('value="ideas:write" checked', edit_form)


class IdeasPageTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password=PASSWORD, email="")
        self.client.force_login(self.admin)
        make = lambda **kw: Idea.objects.create(original_message="msg", **kw)  # noqa: E731
        self.big_small = make(title="Quick big win", impact="high", effort="small", app="resell", tags=["ui"])
        self.big_large = make(title="Big project", impact="high", effort="large", priority="urgent", app="opps")
        self.small_small = make(title="Tiny polish", impact="low", effort="small", category="bug", status="planned")
        self.done = make(title="Already shipped", impact="high", effort="small", status="done")
        self.medium = make(title="Medium thing", impact="medium", effort="medium", tags=["ui", "mobile"],
                           description="Make the header fit phones")

    def titles(self, **params):
        params.setdefault("status", "all")
        return [r["title"] for r in self.client.get(reverse("albright_control:ideas"), params).context["rows"]]

    def test_filters_and_search(self):
        self.assertEqual(self.titles(app="resell"), ["Quick big win"])
        self.assertEqual(self.titles(category="bug"), ["Tiny polish"])
        self.assertEqual(self.titles(priority="urgent"), ["Big project"])
        self.assertEqual(self.titles(status="done"), ["Already shipped"])
        self.assertEqual(sorted(self.titles(tag="ui")), ["Medium thing", "Quick big win"])
        self.assertEqual(self.titles(q="phones"), ["Medium thing"])
        self.assertEqual(self.titles(status="new", tag="ui"), ["Medium thing", "Quick big win"])  # within the tab

    def test_sorts(self):
        self.assertEqual(self.titles()[0], "Medium thing")                        # newest first by default
        self.assertEqual(self.titles(sort="oldest")[0], "Quick big win")
        self.assertEqual(self.titles(sort="priority")[0], "Big project")         # urgent first, by rank

    def test_quick_wins_order(self):
        quick = self.titles(status="new", sort="quick")
        self.assertEqual(quick, ["Quick big win", "Big project", "Medium thing"])   # high impact + small effort first

    def test_add_manual_idea_and_duplicate_hint(self):
        response = self.client.post(reverse("albright_control:idea_add"), {
            "title": "Quick big wins", "description": "d", "app": "all", "category": "feature", "priority": "low",
            "impact": "low", "effort": "small", "tags_text": "x, y", "links_text": "", "target_date": "", "my_notes": "",
            "original_message": ""})
        idea = Idea.objects.get(title="Quick big wins")
        self.assertRedirects(response, reverse("albright_control:idea", args=[idea.pk]))
        self.assertEqual((idea.source, idea.status, idea.tags), ("manual", "new", ["x", "y"]))
        self.assertEqual(idea.possible_duplicate_of, self.big_small)
        self.assertEqual(AuditEntry.objects.get().action, "idea_created")

    def test_detail_edit_status_and_original_message_read_only(self):
        url = reverse("albright_control:idea", args=[self.medium.pk])
        page = self.client.get(url)
        self.assertContains(page, 'id="original-message">msg</pre>')
        self.assertNotIn("original_message", page.context["form"].fields)
        self.client.post(url, {"action": "edit", "title": "Medium thing v2", "description": "<b>bold</b>", "app": "admin",
                               "category": "improvement", "priority": "high", "impact": "high", "effort": "small",
                               "status": "considering", "target_date": "", "my_notes": "my take",
                               "tags_text": "ui", "links_text": "https://example.com/a", "original_message": "HACKED"})
        self.medium.refresh_from_db()
        self.assertEqual((self.medium.title, self.medium.app, self.medium.status, self.medium.my_notes, self.medium.original_message),
                         ("Medium thing v2", "admin", "considering", "my take", "msg"))
        self.assertContains(self.client.get(url), "&lt;b&gt;bold&lt;/b&gt;")
        before = self.medium.status_changed_at
        self.client.post(url, {"action": "status", "status": "in_progress"})
        self.medium.refresh_from_db()
        self.assertEqual(self.medium.status, "in_progress")
        self.assertGreater(self.medium.status_changed_at, before)
        statuses = [e.details for e in AuditEntry.objects.filter(action="idea_status").order_by("pk")]
        self.assertEqual(len(statuses), 2)
        self.assertIn("considering -> in_progress", statuses[1])

    def test_merge_keeps_both_original_messages(self):
        keep = Idea.objects.create(title="Sound alert", original_message="first message", tags=["a"], links=["https://x.dev/1"])
        dup = Idea.objects.create(title="Sound alert!", original_message="second message", tags=["b"],
                                  links=["https://x.dev/2"], possible_duplicate_of=keep, description="extra detail")
        response = self.client.post(reverse("albright_control:idea", args=[dup.pk]), {"action": "merge", "into": keep.pk})
        self.assertRedirects(response, reverse("albright_control:idea", args=[keep.pk]))
        keep.refresh_from_db()
        self.assertFalse(Idea.objects.filter(pk=dup.pk).exists())
        self.assertIn("first message", keep.original_message)
        self.assertIn("second message", keep.original_message)
        self.assertIn(f"Merged from idea #{dup.pk}", keep.original_message)
        self.assertEqual((keep.tags, keep.links), (["a", "b"], ["https://x.dev/1", "https://x.dev/2"]))
        self.assertIn("extra detail", keep.description)
        self.assertEqual(AuditEntry.objects.get().action, "idea_merged")

    def test_delete_is_audited(self):
        self.client.post(reverse("albright_control:idea", args=[self.medium.pk]), {"action": "delete"})
        self.assertFalse(Idea.objects.filter(pk=self.medium.pk).exists())
        self.assertEqual(AuditEntry.objects.get().action, "idea_deleted")

    def test_move_from_bot_suggestions(self):
        suggestion = SiteSuggestion.objects.create(app="opps", area="Income tab", title="Calendar view",
                                                   description="Show deadlines on a calendar.", priority="high",
                                                   my_notes="good one")
        inbox = self.client.get(reverse("albright_control:suggestions"))
        self.assertContains(inbox, reverse("albright_control:suggestion_to_idea", args=[suggestion.pk]))
        response = self.client.post(reverse("albright_control:suggestion_to_idea", args=[suggestion.pk]))
        idea = Idea.objects.get(title="Calendar view")
        self.assertRedirects(response, reverse("albright_control:idea", args=[idea.pk]))
        self.assertEqual((idea.source, idea.app, idea.priority, idea.description, idea.my_notes, idea.status),
                         ("moved_from_suggestion", "opps", "high", "Show deadlines on a calendar.", "good one", "new"))
        self.assertIn(f"Moved from Bot suggestion #{suggestion.pk} - Income tab", idea.original_message)
        self.assertFalse(SiteSuggestion.objects.filter(pk=suggestion.pk).exists())
        self.assertEqual(AuditEntry.objects.get().action, "idea_created")

    def test_prompt_format(self):
        idea = Idea.objects.create(title="Watchlist sound alert", description="Beep under max in the last 10 min.",
                                   app="resell", my_notes="Use the Web Audio API.", original_message="m")
        self.assertEqual(claude_code_prompt(idea),
                         "Watchlist sound alert\n\nApp: The Reselling app (albright_reselling_app).\n\n"
                         "Beep under max in the last 10 min.\n\nMy notes:\nUse the Web Audio API.\n\n" + CLOSING)
        for line in ("python manage.py check and python manage.py test --keepdb", "makemigrations and migrate",
                     "showmigrations shows nothing unapplied"):
            self.assertIn(line, CLOSING)
        page = self.client.get(reverse("albright_control:idea", args=[idea.pk]))
        self.assertContains(page, 'data-copy-target="idea-prompt">Copy as Claude Code prompt</button>')
        self.assertContains(page, "App: The Reselling app (albright_reselling_app).")

    def test_overview_counts_and_nav(self):
        Idea.objects.create(title="Doing it", status="in_progress", original_message="m")
        page = self.client.get(reverse("albright_control:overview"))
        self.assertContains(page, "3 new</span>")       # Quick big win, Big project, Medium thing
        self.assertContains(page, "1 planned &middot; 1 in progress")
        self.assertContains(page, f'<a href="{reverse("albright_control:ideas")}"><span>Ideas</span> '
                                  '<span class="badge warn navbar__badge">3</span></a>')   # Bot group, 3 new

    def test_non_superusers_get_404(self):
        regular = with_apps(User.objects.create_user(username="regular", password=PASSWORD))
        client = Client()
        client.force_login(regular)
        for url in (reverse("albright_control:ideas"), reverse("albright_control:idea_add"),
                    reverse("albright_control:idea", args=[self.medium.pk]),
                    reverse("albright_control:suggestion_to_idea", args=[1])):
            self.assertEqual(client.get(url).status_code, 404)
            self.assertEqual(client.post(url).status_code, 404)

    def test_find_duplicate_ignores_closed_ideas(self):
        self.assertIsNone(find_duplicate("Already shipped"))
        self.assertEqual(find_duplicate("quick BIG win"), self.big_small)
