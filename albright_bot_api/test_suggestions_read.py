"""GET site-suggestions and site-suggestions/counts: the suggestions:read scope (or site:suggest for a token's
own), filters, include=description, paging, the private notes never leaving, and the activity log."""
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from .models import BotRequestLog, BotToken, SiteSuggestion
from .tests import BotApiTestCase

LIST = reverse("bot_api_v1:site_suggestions")
COUNTS = reverse("bot_api_v1:site_suggestion_counts")


class SuggestionsReadTests(BotApiTestCase):
    def setUp(self):
        super().setUp()
        self.reader, self.reader_raw = BotToken.issue(self.user, "Reader bot", ["suggestions:read"])
        self.poster, self.poster_raw = BotToken.issue(self.user, "Poster bot", ["site:suggest"])
        mk = lambda token, title, **f: SiteSuggestion.objects.create(
            token=token, title=title, description=f"About {title}", my_notes=f"SECRET note on {title}",
            **{"app": "all", **f})
        self.new = mk(self.poster, "Calendar view", app="opps", priority="high")
        self.planned = mk(self.poster, "Dark charts", status="planned", app="trade")
        self.doing = mk(self.reader, "Faster search", status="in_progress", app="resell", priority="low")
        self.other = mk(None, "Old idea", status="new")          # its token was deleted

    def get(self, path, raw, **params):
        return self.call("GET", path + ("?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""),
                         raw=raw)

    def ids(self, response):
        self.assertEqual(response.status_code, 200, response.content)
        return [row["id"] for row in response.json()["suggestions"]]

    # scope ---------------------------------------------------------------
    def test_scope_required(self):
        none, none_raw = BotToken.issue(self.user, "No scopes", ["ideas:read"])
        for path in (LIST, COUNTS):
            response = self.get(path, none_raw)
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json(), {"error": "missing scope", "required": "suggestions:read"})
            self.assertEqual(self.get(path, self.reader_raw).status_code, 200)
        # site:suggest alone may list its own, but not see the counts
        self.assertEqual(self.get(COUNTS, self.poster_raw).json(),
                         {"error": "missing scope", "required": "suggestions:read"})

    def test_reader_sees_every_bot_suggestion_newest_first(self):
        response = self.get(LIST, self.reader_raw)
        self.assertEqual(self.ids(response), [self.other.pk, self.doing.pk, self.planned.pk, self.new.pk])
        data = response.json()
        self.assertFalse(data["own_only"])
        self.assertEqual(data["total"], 4)
        row = data["suggestions"][-1]
        self.assertEqual(set(row), {"id", "title", "app", "priority", "status", "source", "created_at",
                                    "status_changed_at"})
        self.assertEqual((row["source"], row["priority"], row["app"]), ("Poster bot", "high", "opps"))
        self.assertIsNotNone(row["status_changed_at"])
        self.assertIsNone(data["suggestions"][0]["source"])

    def test_site_suggest_alone_sees_only_its_own(self):
        response = self.get(LIST, self.poster_raw)
        self.assertEqual(self.ids(response), [self.planned.pk, self.new.pk])
        self.assertTrue(response.json()["own_only"])
        self.assertEqual(self.ids(self.get(LIST, self.poster_raw, status="in_progress")), [])

    def test_posting_still_works_on_the_same_path(self):
        response = self.call("POST", LIST, {"app": "all", "title": "New one", "description": "x"},
                             raw=self.poster_raw)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.call("POST", LIST, {"app": "all", "title": "x", "description": "x"},
                                   raw=self.reader_raw).status_code, 403)   # reading doesn't allow posting

    # filters -------------------------------------------------------------
    def test_status_filter_one_and_several(self):
        self.assertEqual(self.ids(self.get(LIST, self.reader_raw, status="new")), [self.other.pk, self.new.pk])
        self.assertEqual(self.ids(self.get(LIST, self.reader_raw, status="planned,in_progress")),
                         [self.doing.pk, self.planned.pk])
        response = self.get(LIST, self.reader_raw, status="planned,accepted")
        self.assertEqual(response.status_code, 400)
        self.assertIn("in_progress", response.json()["error"])

    def test_app_priority_query_and_dates(self):
        self.assertEqual(self.ids(self.get(LIST, self.reader_raw, app="trade,resell")),
                         [self.doing.pk, self.planned.pk])
        self.assertEqual(self.ids(self.get(LIST, self.reader_raw, priority="high")), [self.new.pk])
        self.assertEqual(self.ids(self.get(LIST, self.reader_raw, q="CHARTS")), [self.planned.pk])
        long_ago = timezone.now() - timedelta(days=30)
        SiteSuggestion.objects.filter(pk=self.new.pk).update(created_at=long_ago, updated_at=long_ago)
        since = (timezone.localdate() - timedelta(days=1)).isoformat()
        self.assertNotIn(self.new.pk, self.ids(self.get(LIST, self.reader_raw, created_since=since)))
        self.assertNotIn(self.new.pk, self.ids(self.get(LIST, self.reader_raw, updated_since=since)))
        self.assertEqual(len(self.ids(self.get(LIST, self.reader_raw, created_since="2000-01-01T00:00:00"))), 4)
        self.assertEqual(self.get(LIST, self.reader_raw, created_since="yesterday").status_code, 400)
        self.assertEqual(self.get(LIST, self.reader_raw, priority="urgent").status_code, 400)

    def test_include_description(self):
        rows = self.get(LIST, self.reader_raw).json()["suggestions"]
        self.assertTrue(all("description" not in row for row in rows))
        rows = self.get(LIST, self.reader_raw, include="description").json()["suggestions"]
        self.assertEqual({row["title"]: row["description"] for row in rows}["Calendar view"], "About Calendar view")
        self.assertEqual(self.get(LIST, self.reader_raw, include="my_notes").status_code, 400)

    def test_private_notes_never_returned(self):
        for raw in (self.reader_raw, self.poster_raw):
            for params in ({}, {"include": "description"}, {"q": "SECRET"}):
                response = self.get(LIST, raw, **params)
                self.assertNotIn("SECRET", response.content.decode())
                self.assertNotIn("my_notes", response.content.decode())
        self.assertEqual(self.ids(self.get(LIST, self.reader_raw, q="SECRET")), [])   # not searched either

    def test_sort_by_status_change_and_paging(self):
        self.new.status = "considering"
        self.new.save(update_fields=["status", "updated_at"])         # how Admin saves a move
        self.new.refresh_from_db()
        self.assertGreater(self.new.status_changed_at, self.other.status_changed_at)
        self.assertEqual(self.ids(self.get(LIST, self.reader_raw, sort="status_changed_at"))[0], self.new.pk)
        self.assertEqual(self.get(LIST, self.reader_raw, sort="title").status_code, 400)
        first = self.get(LIST, self.reader_raw, page_size=3).json()
        self.assertEqual((len(first["suggestions"]), first["pages"], first["next_page"]), (3, 2, 2))
        second = self.get(LIST, self.reader_raw, page_size=3, page=2).json()
        self.assertEqual((len(second["suggestions"]), second["next_page"]), (1, None))
        self.assertEqual(self.get(LIST, self.reader_raw, page_size=101).status_code, 400)
        self.assertEqual(self.get(LIST, self.reader_raw, page="x").status_code, 400)

    def test_status_changed_at_only_moves_on_a_status_change(self):
        before = self.planned.status_changed_at
        self.planned.my_notes = "edited"
        self.planned.save(update_fields=["my_notes", "updated_at"])
        self.planned.refresh_from_db()
        self.assertEqual(self.planned.status_changed_at, before)

    # counts --------------------------------------------------------------
    def test_counts(self):
        response = self.get(COUNTS, self.reader_raw)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"counts": {"new": 2, "considering": 0, "planned": 1, "in_progress": 1,
                                                      "done": 0, "declined": 0}, "total": 4})

    # log -----------------------------------------------------------------
    def test_calls_are_logged(self):
        self.get(LIST, self.reader_raw, status="new")
        self.get(COUNTS, self.poster_raw)
        logs = list(BotRequestLog.objects.order_by("pk").values_list("token__name", "status", "summary"))
        self.assertEqual(logs[-2][:2], ("Reader bot", 200))
        self.assertIn("listed 2 site suggestions", logs[-2][2])
        self.assertEqual(logs[-1], ("Poster bot", 403, "rejected: needs suggestions:read"))
