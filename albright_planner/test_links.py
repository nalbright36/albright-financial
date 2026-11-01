"""The Planner's links to the other apps: the Meals overlay (view only, only for people who can open Meals,
remembered toggle), notes linked both ways (attach / new note / "Add to schedule", deletions, permissions),
the shared upsert helper (no duplicates), each source and its on/off switch, and the Bot API's schedule
endpoints (scopes, labels, completion, the activity log)."""
import json
from datetime import time, timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import Client, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotRequestLog, BotToken
from albright_labs.testing import allow_api_keys, with_apps
from albright_meals.models import MealPlanEntry
from albright_notes.models import Note, NoteShare

from . import services, sources
from .models import ItemCompletion, PlannerSettings, ScheduleItem
from .tests import PASSWORD, TODAY, Base, url


def meal(day=TODAY, slot="dinner", title="Tacos", **fields):
    return MealPlanEntry.objects.create(date=day, slot=slot, title=title, **fields)


# ---------------------------------------------------------------------------
# Meals overlay
# ---------------------------------------------------------------------------

class MealsOverlayTests(Base):
    def page(self, view, client=None):
        return (client or self.client).get(url(view) + f"?date={TODAY.isoformat()}").content.decode()

    def test_planned_meals_show_in_day_week_and_month_view_only(self):
        entry = meal()
        for view in ("day", "week", "month"):
            with self.subTest(view=view):
                html = self.page(view)
                self.assertIn("Tacos", html)
                self.assertIn(reverse("albright_meals:plan_entry", args=[entry.pk]), html)   # opens it in Meals
                self.assertIn("is-meal", html)
        day = self.page("day")
        li = day.split(f'data-meal="{entry.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertNotIn("pl-check", li)                   # no checkbox
        self.assertNotIn("draggable", li)                  # no drag
        week = self.page("week")
        li = week.split(f'data-meal="{entry.pk}"', 1)[0].rsplit("<li", 1)[1]
        self.assertNotIn("draggable", li)

    def test_meals_are_read_live_never_copied(self):
        entry = meal()
        self.page("week")
        self.assertFalse(ScheduleItem.objects.exists())
        entry.title = "Pizza"
        entry.save()
        self.assertIn("Pizza", self.page("day"))

    def test_skipped_meals_are_left_out(self):
        meal(title="Skipped soup", status="skipped")
        self.assertNotIn("Skipped soup", self.page("day"))

    def test_only_people_who_can_open_meals_see_them(self):
        meal()
        client = self.as_user(self.wife)                   # Planner only - no Meals access
        html = self.page("day", client)
        self.assertNotIn("Tacos", html)
        self.assertNotIn("Show meals", html)
        self.assertEqual(sources.meals(self.wife, TODAY, TODAY), [])

    def test_show_meals_toggle_is_remembered(self):
        meal()
        self.assertIn("Show meals", self.page("day"))
        self.client.post(url("toggle_meals"), {"show": "0", "next": url("day")})
        self.assertFalse(PlannerSettings.objects.get(user=self.me).show_meals)
        for view in ("day", "week", "month"):
            self.assertNotIn("Tacos", self.page(view))
        self.client.post(url("toggle_meals"), {"show": "1"})
        self.assertIn("Tacos", self.page("day"))


# ---------------------------------------------------------------------------
# Notes, both ways
# ---------------------------------------------------------------------------

class NoteLinkTests(Base):
    def setUp(self):
        super().setUp()
        self.note = Note.objects.create(owner=self.me, title="Dentist questions", body="Ask about the crown\n\nmore")
        self.task = self.item("Call dentist")

    def edit_page(self, item=None, client=None):
        return (client or self.client).get(url("edit", (item or self.task).pk)).content.decode()

    def test_attach_an_existing_note_shows_its_title_and_preview(self):
        page = self.edit_page()
        self.assertIn("Attach note", page)
        self.assertIn("Dentist questions", page)              # offered in the picker
        self.client.post(url("item_note", self.task.pk), {"action": "attach", "note": self.note.pk})
        self.task.refresh_from_db()
        self.assertEqual(self.task.note, self.note)
        page = self.edit_page()
        self.assertIn('id="pl-linked-note"', page)
        self.assertIn("Ask about the crown", page)
        self.assertIn(reverse("albright_notes:note", args=[self.note.pk]), page)
        # Edits in Notes show here - the item stores only the note's id.
        self.note.body = "Bring insurance card"
        self.note.save()
        self.assertIn("Bring insurance card", self.edit_page())
        self.assertIn("&#x1F4DD; Note", self.client.get(url("day") + f"?date={TODAY}").content.decode())

    def test_new_note_is_owned_by_the_user_titled_from_the_item_and_linked(self):
        response = self.client.post(url("item_note", self.task.pk), {"action": "new"})
        note = Note.objects.exclude(pk=self.note.pk).get()
        self.assertEqual((note.owner, note.title), (self.me, "Call dentist"))
        self.assertRedirects(response, reverse("albright_notes:note", args=[note.pk]), fetch_redirect_response=False)
        self.task.refresh_from_db()
        self.assertEqual(self.task.note, note)

    def test_can_only_attach_notes_you_can_see(self):
        theirs = Note.objects.create(owner=self.stranger, title="Private")
        self.assertNotIn("Private", self.edit_page())
        self.client.post(url("item_note", self.task.pk), {"action": "attach", "note": theirs.pk})
        self.task.refresh_from_db()
        self.assertIsNone(self.task.note)
        NoteShare.objects.create(note=theirs, user=self.me)          # shared with me: now it's attachable
        self.client.post(url("item_note", self.task.pk), {"action": "attach", "note": theirs.pk})
        self.task.refresh_from_db()
        self.assertEqual(self.task.note, theirs)

    def test_no_notes_access_means_no_attaching(self):
        item = self.item("Hers", owner=self.wife)
        client = self.as_user(self.wife)                             # Planner only
        self.assertNotIn("Attach note", self.edit_page(item, client))
        self.assertEqual(client.post(url("item_note", item.pk), {"action": "new"}).status_code, 404)
        self.assertFalse(Note.objects.filter(owner=self.wife).exists())

    def test_a_viewer_who_cant_see_the_note_sees_only_that_one_is_linked(self):
        self.task.note = self.note
        self.task.save()
        self.task.shares.create(user=self.wife)
        page = self.edit_page(client=self.as_user(self.wife))
        self.assertIn("isn't shared with you", page)
        self.assertNotIn("Ask about the crown", page)

    def test_deleting_the_note_keeps_the_item_and_shows_note_deleted(self):
        self.task.note = self.note
        self.task.save()
        self.client.post(reverse("albright_notes:delete", args=[self.note.pk]))
        self.task.refresh_from_db()
        self.assertEqual(self.task.title, "Call dentist")
        self.assertIsNone(self.task.note)
        self.assertTrue(self.task.note_deleted)
        self.assertIn("Note deleted", self.edit_page())
        self.assertIn("Note deleted", self.client.get(url("day") + f"?date={TODAY}").content.decode())

    def test_deleting_the_item_keeps_the_note(self):
        self.task.note = self.note
        self.task.save()
        self.client.post(url("remove", self.task.pk), {"scope": "all"})
        self.assertFalse(ScheduleItem.objects.filter(pk=self.task.pk).exists())
        self.assertTrue(Note.objects.filter(pk=self.note.pk).exists())

    def test_add_to_schedule_from_a_note(self):
        page = self.client.get(reverse("albright_notes:note", args=[self.note.pk])).content.decode()
        self.assertIn("Add to schedule", page)
        response = self.client.post(url("from_note", self.note.pk),
                                    {"date": "2026-11-10", "time": "15:00", "repeat": "weekly"})
        self.assertRedirects(response, reverse("albright_notes:note", args=[self.note.pk]), fetch_redirect_response=False)
        item = ScheduleItem.objects.get(note=self.note)
        self.assertEqual((item.owner, item.title, item.date, item.start_time, item.recurrence),
                         (self.me, "Dentist questions", TODAY + timedelta(days=6), time(15), "FREQ=WEEKLY;BYDAY=TU"))
        page = self.client.get(reverse("albright_notes:note", args=[self.note.pk])).content.decode()
        self.assertIn("Scheduled: Tue Nov 10", page)
        self.assertIn(url("day") + "?date=2026-11-10", page)

    def test_add_to_schedule_needs_a_visible_note_and_planner_access(self):
        theirs = Note.objects.create(owner=self.stranger, title="Private")
        self.assertEqual(self.client.post(url("from_note", theirs.pk), {"date": "2026-11-10"}).status_code, 404)
        notes_only = with_apps(User.objects.create_user(username="reader", password=PASSWORD), ["notes"])
        mine = Note.objects.create(owner=notes_only, title="No planner")
        client = self.as_user(notes_only)
        self.assertNotIn("Add to schedule", client.get(reverse("albright_notes:note", args=[mine.pk])).content.decode())
        client.post(url("from_note", mine.pk), {"date": "2026-11-10"})
        self.assertFalse(ScheduleItem.objects.filter(note=mine).exists())


# ---------------------------------------------------------------------------
# The shared upsert helper and the sources
# ---------------------------------------------------------------------------

class UpsertTests(Base):
    def test_upsert_updates_in_place_instead_of_duplicating(self):
        item, created = services.upsert_source_item(self.me, "opportunities", "deadline:1", "Apply by: A", TODAY,
                                                    url="/opportunities/opportunity/1/")
        self.assertTrue(created)
        again, created = services.upsert_source_item(self.me, "opportunities", "deadline:1", "Apply by: A (edited)",
                                                     TODAY + timedelta(days=1), time(9), url="/opportunities/opportunity/1/")
        self.assertFalse(created)
        self.assertEqual(again.pk, item.pk)
        self.assertEqual(ScheduleItem.objects.filter(source_app="opportunities").count(), 1)
        item.refresh_from_db()
        self.assertEqual((item.title, item.date, item.start_time), ("Apply by: A (edited)", TODAY + timedelta(days=1), time(9)))
        # Another person gets their own copy of the same record.
        services.upsert_source_item(self.wife, "opportunities", "deadline:1", "Apply by: A", TODAY)
        self.assertEqual(ScheduleItem.objects.filter(source_app="opportunities").count(), 2)

    def test_stale_items_go_but_checked_off_ones_stay(self):
        gone, _ = services.upsert_source_item(self.me, "markdown", "1:14", "Gone", TODAY)
        done, _ = services.upsert_source_item(self.me, "markdown", "2:14", "Done", TODAY)
        kept, _ = services.upsert_source_item(self.me, "markdown", "3:14", "Kept", TODAY)
        ItemCompletion.objects.create(item=done, occurrence_date=TODAY)
        services.remove_source_items(self.me, "markdown", keep=["3:14"])
        self.assertEqual(set(ScheduleItem.objects.values_list("pk", flat=True)), {done.pk, kept.pk})

    def test_source_item_links_back(self):
        # (an app outside sources.SOURCES calling the helper directly - sync() leaves its items alone)
        services.upsert_source_item(self.me, "someapp", "9", "From another app", TODAY,
                                    url="/opportunities/opportunity/9/")
        html = self.client.get(url("day") + f"?date={TODAY}").content.decode()
        self.assertIn('class="pl-src"', html)
        self.assertIn('href="/opportunities/opportunity/9/"', html)


class SourceTests(Base):
    """How each source behaves once it's turned ON (they're all off by default - see test_no_auto_items)."""
    def setUp(self):
        super().setUp()
        self.today = timezone.localdate()
        for user in (self.me, self.wife):
            PlannerSettings.objects.update_or_create(user=user, defaults={"sources_on": list(sources.SOURCE_KEYS)})

    def sync(self, user=None):
        return sources.sync(user or self.me, today=self.today)

    def turn(self, key, on):
        prefs = PlannerSettings.for_user(self.me)
        prefs.sources_on = [k for k in prefs.sources_on if k != key] + ([key] if on else [])
        prefs.save()

    def watched_lot(self):
        from albright_reselling_app.models import BidWatch, SourcedLot
        lot = SourcedLot.objects.create(source="shopgoodwill", external_id="w1", url="https://shopgoodwill.com/item/w1",
                                        title="Morgan dollar lot", end_time=timezone.now() + timedelta(days=1))
        BidWatch.objects.create(lot=lot)
        return lot

    def test_watchlist_auctions_are_view_only_events_linking_to_the_lot(self):
        lot = self.watched_lot()
        self.assertEqual(self.sync()["watchlist"], 1)
        item = ScheduleItem.objects.get(source_app="watchlist")
        self.assertEqual((item.kind, item.checkable, item.title, item.source_id),
                         ("event", False, "Auction ends: Morgan dollar lot", str(lot.pk)))
        self.assertEqual(item.date, timezone.localtime(lot.end_time).date())
        self.assertTrue(item.locked)
        self.assertFalse(item.can_edit(self.me))
        self.assertEqual(self.client.post(url("move", item.pk), {"date": item.date, "to": item.date + timedelta(days=1)},
                                          content_type="application/json").status_code, 404)
        self.assertEqual(self.client.post(url("edit", item.pk), {"title": "x"}).status_code, 404)
        html = self.client.get(url("week") + f"?date={item.date}").content.decode()
        li = html.split(f'data-item="{item.pk}"', 1)[1].split("</li>", 1)[0]
        self.assertNotIn("draggable", li)
        self.assertIn("target=\"_blank\"", li)
        # Same lot next sync: updated, not duplicated; off the watchlist: gone.
        lot.title = "Morgan dollar lot (relisted)"
        lot.save()
        self.sync()
        self.assertEqual(ScheduleItem.objects.get(source_app="watchlist").title, "Auction ends: Morgan dollar lot (relisted)")
        lot.bid_watch.delete()
        self.sync()
        self.assertFalse(ScheduleItem.objects.filter(source_app="watchlist").exists())

    def test_opportunity_deadlines_and_follow_ups_are_tasks(self):
        from albright_crypto_app.models import Opportunity, Source
        source = Source.objects.create(kind="rss", name="feed", url="https://example.org/feed")
        saved = Opportunity.objects.create(source=source, external_id="1", url="https://example.org/1", title="Grant",
                                           status="saved", application_deadline=timezone.now() + timedelta(days=3))
        applied = Opportunity.objects.create(source=source, external_id="2", url="https://example.org/2", title="Job",
                                             status="applied", applied_at=timezone.now() - timedelta(days=8),
                                             stage_changed_at=timezone.now() - timedelta(days=8))
        Opportunity.objects.create(source=source, external_id="3", url="https://example.org/3", title="Inbox one",
                                   status="inbox", application_deadline=timezone.now() + timedelta(days=3))
        self.assertEqual(self.sync()["opportunities"], 2)
        deadline = ScheduleItem.objects.get(source_id=f"deadline:{saved.pk}")
        follow = ScheduleItem.objects.get(source_id=f"followup:{applied.pk}")
        self.assertEqual((deadline.title, deadline.kind, deadline.checkable), ("Apply by: Grant", "task", True))
        self.assertEqual(deadline.source_url, reverse("albright_crypto_app:opportunity", args=[saved.pk]))
        self.assertEqual(follow.title, "Follow up: Job")
        self.assertEqual(follow.date, timezone.localtime(applied.stage_changed_at + timedelta(days=7)).date())
        self.assertTrue(follow.can_edit(self.me))                       # a task: check it off like any other

    def test_due_markdown_steps_are_tasks(self):
        from albright_reselling_app.models import LedgerEntry
        entry = LedgerEntry.objects.create(owner=self.me, item="Silver spoon", cost=Decimal("5"), status="holding",
                                           listed_at=self.today - timedelta(days=20), list_price=Decimal("30"))
        LedgerEntry.objects.create(owner=self.me, item="Fresh", cost=Decimal("5"), status="holding",
                                   listed_at=self.today - timedelta(days=2))
        LedgerEntry.objects.create(owner=self.wife, item="Hers", cost=Decimal("5"), status="holding",
                                   listed_at=self.today - timedelta(days=20))
        self.assertEqual(self.sync()["markdown"], 1)
        item = ScheduleItem.objects.get(source_app="markdown")
        self.assertEqual((item.title, item.date, item.checkable, item.source_id),
                         ("Send offers to watchers: Silver spoon", entry.listed_at + timedelta(days=14), True, f"{entry.pk}:14"))
        self.assertEqual(item.source_url, reverse("albright_reselling_app:ledger_manage", args=[entry.pk]))

    def test_each_source_can_be_turned_off_and_on(self):
        from albright_crypto_app.models import Opportunity, Source
        from albright_reselling_app.models import LedgerEntry
        self.watched_lot()
        source = Source.objects.create(kind="rss", name="feed", url="https://example.org/feed")
        Opportunity.objects.create(source=source, external_id="1", url="https://example.org/1", title="Grant",
                                   status="saved", application_deadline=timezone.now() + timedelta(days=3))
        LedgerEntry.objects.create(owner=self.me, item="Spoon", cost=Decimal("5"), status="holding",
                                   listed_at=self.today - timedelta(days=20))
        self.sync()
        for key in sources.SOURCE_KEYS:
            with self.subTest(source=key):
                self.turn(key, False)
                self.sync()
                self.assertFalse(ScheduleItem.objects.filter(source_app=key, archived=False).exists())
                others = [k for k in sources.SOURCE_KEYS if k != key]
                self.assertEqual(set(ScheduleItem.objects.filter(archived=False).values_list("source_app", flat=True)),
                                 set(others))
                self.turn(key, True)
                self.sync()
                self.assertEqual(ScheduleItem.objects.filter(source_app=key, archived=False).count(), 1)

    def test_settings_page_turns_sources_on_and_off(self):
        page = self.client.get(url("settings")).content.decode()
        for source in sources.SOURCES:
            self.assertIn(f'value="{source.key}"', page)
        self.client.post(url("settings"), {"source": ["opportunities"], "show_meals": "1"})
        prefs = PlannerSettings.objects.get(user=self.me)
        self.assertEqual(prefs.sources_on, ["opportunities"])
        self.assertTrue(prefs.show_meals)

    def test_sources_need_access_to_their_app(self):
        self.watched_lot()
        self.sync(self.wife)                                            # Planner only
        self.assertFalse(ScheduleItem.objects.filter(owner=self.wife).exists())
        self.assertNotIn('value="watchlist"', self.as_user(self.wife).get(url("settings")).content.decode())

    def test_only_saving_settings_syncs_never_a_page_load(self):
        self.watched_lot()
        for view in ("day", "week", "month"):
            self.client.get(url(view))
        self.assertFalse(ScheduleItem.objects.exists())
        self.client.post(url("settings"), {"source": list(sources.SOURCE_KEYS), "show_meals": "1"})
        self.assertEqual(ScheduleItem.objects.filter(source_app="watchlist").count(), 1)


# ---------------------------------------------------------------------------
# Bot API
# ---------------------------------------------------------------------------

@override_settings(BOT_API_REQUIRE_HTTPS=False)
class ScheduleApiTests(Base):
    def setUp(self):
        super().setUp()
        allow_api_keys(self.me)
        self.token, self.raw = BotToken.issue(self.me, "Grok bot", ["schedule:read", "schedule:write"])
        self.api = Client()

    def call(self, method, path, body=None, raw=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw or self.raw}"}
        if method == "GET":
            return self.api.get(path, **headers)
        return self.api.post(path, data=json.dumps(body or {}), content_type="application/json", **headers)

    def api_url(self, name, *args):
        return reverse(f"bot_api_v1:{name}", args=args)

    def test_scopes_are_required(self):
        _token, raw = BotToken.issue(self.me, "Reader", ["notes:read"])
        self.assertEqual(self.call("GET", self.api_url("schedule"), raw=raw).status_code, 403)
        self.assertEqual(self.call("POST", self.api_url("schedule"), {"title": "x", "date": "2026-11-05"}, raw=raw).status_code, 403)
        item = self.item("Task")
        self.assertEqual(self.call("POST", self.api_url("schedule_complete", item.pk), {}, raw=raw).status_code, 403)
        _token, read_only = BotToken.issue(self.me, "Read only", ["schedule:read"])
        self.assertEqual(self.call("GET", self.api_url("schedule"), raw=read_only).status_code, 200)
        self.assertEqual(self.call("POST", self.api_url("schedule"), {"title": "x", "date": "2026-11-05"},
                                   raw=read_only).status_code, 403)
        self.assertFalse(ScheduleItem.objects.filter(title="x").exists())

    def test_list_occurrences_with_completion_state(self):
        gym = self.item("Gym", date=TODAY - timedelta(days=2), recurrence="FREQ=WEEKLY;BYDAY=MO,WE,FR", start_time=time(6))
        ItemCompletion.objects.create(item=gym, occurrence_date=TODAY)
        response = self.call("GET", self.api_url("schedule") + "?start=2026-11-02&end=2026-11-08")
        self.assertEqual(response.status_code, 200)
        rows = response.json()["occurrences"]
        self.assertEqual([(r["date"], r["done"]) for r in rows],
                         [("2026-11-02", False), ("2026-11-04", True), ("2026-11-06", False)])
        self.assertEqual(rows[0]["start_time"], "06:00")
        self.assertEqual(self.call("GET", self.api_url("schedule") + "?start=2026-11-08&end=2026-11-02").status_code, 400)
        self.assertEqual(self.call("GET", self.api_url("schedule") + "?start=2026-01-01&end=2027-06-01").status_code, 400)

    def test_create_with_simple_repeat_is_labeled_with_the_bot(self):
        response = self.call("POST", self.api_url("schedule"), {
            "title": "Gym", "date": "2026-11-02", "start_time": "06:00", "repeat": "weekly", "days": ["MO", "WE", "FR"],
            "count": 10})
        self.assertEqual(response.status_code, 201, response.content)
        item = ScheduleItem.objects.get(pk=response.json()["id"])
        self.assertEqual((item.owner, item.recurrence, item.recurrence_count, item.created_by, item.from_bot, item.bot_token),
                         (self.me, "FREQ=WEEKLY;BYDAY=MO,WE,FR", 10, "Grok bot", True, self.token))
        self.assertEqual(response.json()["repeats"], "Every Mon, Wed, Fri, 10 times")
        html = self.client.get(url("day") + "?date=2026-11-02").content.decode()
        self.assertIn("&#x1F916; Grok bot", html)

    def test_create_with_an_rrule(self):
        response = self.call("POST", self.api_url("schedule"), {
            "title": "Book club", "date": "2026-11-10", "kind": "event", "rrule": "RRULE:FREQ=MONTHLY;BYDAY=2TU;UNTIL=20270630"})
        self.assertEqual(response.status_code, 201, response.content)
        item = ScheduleItem.objects.get(title="Book club")
        self.assertEqual((item.recurrence, str(item.recurrence_until), item.kind, item.checkable),
                         ("FREQ=MONTHLY;BYDAY=2TU", "2027-06-30", "event", False))
        for bad in ({"title": "x", "date": "2026-11-10", "rrule": "FREQ=SECONDLY"},
                    {"title": "x", "date": "2026-11-10", "rrule": "FREQ=WEEKLY;DTSTART=20260101"},
                    {"title": "x", "date": "2026-11-10", "rrule": "FREQ=WEEKLY;BYDAY=XX"},
                    {"title": "x", "date": "2026-11-10", "repeat": "fortnightly"},
                    {"title": "x", "date": "nope"},
                    {"date": "2026-11-10"}):
            with self.subTest(body=bad):
                self.assertEqual(self.call("POST", self.api_url("schedule"), bad).status_code, 400)
        self.assertFalse(ScheduleItem.objects.filter(title="x").exists())

    def test_complete_and_uncomplete_one_occurrence(self):
        gym = self.item("Gym", recurrence="FREQ=WEEKLY;BYDAY=MO,WE,FR")
        response = self.call("POST", self.api_url("schedule_complete", gym.pk), {"occurrence_date": "2026-11-06"})
        self.assertEqual(response.json(), {"item_id": gym.pk, "occurrence_date": "2026-11-06", "done": True})
        self.assertEqual(list(gym.completions.values_list("occurrence_date", flat=True)), [TODAY + timedelta(days=2)])
        self.assertEqual(self.call("POST", self.api_url("schedule_complete", gym.pk), {"occurrence_date": "2026-11-05"}).status_code, 400)
        self.assertEqual(self.call("POST", self.api_url("schedule_complete", gym.pk), {}).status_code, 400)   # repeating: date needed
        self.call("POST", self.api_url("schedule_uncomplete", gym.pk), {"occurrence_date": "2026-11-06"})
        self.assertFalse(gym.completions.exists())
        one_off = self.item("Once")
        self.assertTrue(self.call("POST", self.api_url("schedule_complete", one_off.pk), {}).json()["done"])

    def test_cant_touch_other_peoples_items(self):
        theirs = self.item("Theirs", owner=self.stranger)
        self.assertEqual(self.call("POST", self.api_url("schedule_complete", theirs.pk), {}).status_code, 404)
        self.assertFalse(ItemCompletion.objects.exists())
        self.assertNotIn("Theirs", self.call("GET", self.api_url("schedule")).content.decode())

    def test_owner_without_planner_access_is_refused(self):
        with_apps(self.me, ["notes"])
        self.assertEqual(self.call("GET", self.api_url("schedule")).status_code, 403)
        self.assertEqual(self.call("POST", self.api_url("schedule"), {"title": "x", "date": "2026-11-05"}).status_code, 403)

    def test_every_call_is_logged_and_documented(self):
        BotRequestLog.objects.all().delete()
        self.call("GET", self.api_url("schedule"))
        self.call("POST", self.api_url("schedule"), {"title": "Logged", "date": "2026-11-05"})
        self.call("POST", self.api_url("schedule"), {"date": "2026-11-05"})
        logs = list(BotRequestLog.objects.order_by("pk").values_list("endpoint", "status"))
        self.assertEqual([s for _e, s in logs], [200, 201, 400])
        self.assertTrue(all(e.startswith("/api/bot/v1/schedule") for e, _s in logs))
        docs = self.call("GET", self.api_url("docs")).content.decode()
        for text in ("schedule:read", "schedule:write", "POST schedule/<id>/complete", "GET  schedule?start="):
            self.assertIn(text, docs)
