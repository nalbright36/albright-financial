"""Deleting and archiving programs: owner only; built-in templates can't be deleted; logged workouts are kept with the
program's names; its Planner tasks end or stay (history kept either way); shares removed; archive / restore; the
Bot API's DELETE."""
import json
from datetime import timedelta

from django.contrib.contenttypes.models import ContentType
from django.test import Client

from albright_bot_api.models import BotToken
from albright_labs.models import ActivityEvent, ShareGrant
from albright_planner import services
from albright_planner.models import ItemCompletion, ScheduleItem

from . import programs, stats
from .models import Exercise, Program, ProgramDay, ScheduledSession, Workout
from .test_body import API, Base, url

EVERY_DAY = list(range(7))


class ProgramDeleteBase(Base):
    def make_program(self, owner=None, name="Split", planner=True):
        """A running program with one every-day workout (so today is always an occurrence), on the Planner from two
        weeks ago, with a logged workout and a checked-off past occurrence."""
        owner = owner or self.me
        start = self.today - timedelta(days=14)
        p = Program.objects.create(owner=owner, name=name, start_date=start, active=True)
        day = ProgramDay.objects.create(program=p, name="Upper A", weekdays=EVERY_DAY)
        programs.add_exercise(day, Exercise.objects.get(name="Barbell Bench Press"))
        if planner:
            programs.schedule(p, start=start)
        return p, day

    def day_item(self, day):
        return ScheduleItem.objects.get(owner=self.me, source_app="fitness", source_id=f"day-{day.pk}")

    def check_off(self, item, when):
        ItemCompletion.objects.create(item=item, occurrence_date=when)

    def one_off(self, day, when):
        """An older one-off "Schedule this day" session with its own Planner task."""
        session = ScheduledSession.objects.create(owner=self.me, program_day=day)
        session.planner_item = services.create_scheduled_task(self.me, "fitness", session.source_id, "Upper A", when)
        session.save(update_fields=["planner_item"])
        return session.planner_item


class DeleteTests(ProgramDeleteBase):
    def test_only_the_owner_can_delete_or_archive(self):
        p, _day = self.make_program()
        self.client.force_login(self.wife)
        self.assertEqual(self.client.get(url("program_delete", p.pk)).status_code, 404)
        self.assertEqual(self.client.post(url("program_delete", p.pk), {"remove_scheduled": "1"}).status_code, 404)
        self.assertEqual(self.client.post(url("program_archive", p.pk)).status_code, 404)
        self.assertEqual(self.client.post(url("program", p.pk), {"action": "pause"}).status_code, 404)
        p.refresh_from_db()
        self.assertEqual((p.active, p.archived_at), (True, None))
        self.assertNotIn(f"Options for {p.name}", self.client.get(url("programs")).content.decode())

    def test_the_menu_dialog_and_confirmation_page(self):
        p, _day = self.make_program()
        page = self.client.get(url("program", p.pk)).content.decode()
        self.assertIn('<details class="fx-program-menu" data-menu><summary class="fx-icon-btn" aria-label="Options for Split"', page)
        self.assertIn(f'<a href="{url("program_delete", p.pk)}" class="menu-item menu-item--danger" data-program-delete data-name="Split">Delete program</a>', page)
        self.assertIn('data-program-archive>Archive</button>', page)
        self.assertIn('<dialog class="fx-confirm" id="fx-program-delete"', page)
        self.assertIn('<input type="checkbox" name="remove_scheduled" value="1" checked> Also remove its upcoming scheduled sessions from my Planner', page)
        self.assertIn('<button type="submit" class="btn btn-danger">Delete</button>', page)
        self.assertIn("data-program-delete-cancel>Cancel</button>", page)
        listing = self.client.get(url("programs")).content.decode()
        self.assertIn(f'<div class="fx-row fx-program-row" id="program-{p.pk}">', listing)
        self.assertIn('data-name="Split">Delete program</a>', listing)
        confirm = self.client.get(url("program_delete", p.pk))          # without JS
        self.assertContains(confirm, "Delete Split?")
        self.assertContains(confirm, 'name="remove_scheduled" value="1" checked')
        self.assertTrue(Program.objects.filter(pk=p.pk).exists())      # a GET never deletes

    def test_built_in_templates_cant_be_deleted(self):
        before = dict(programs.TEMPLATES)
        page = self.client.get(url("program_templates")).content.decode()
        self.assertNotIn("data-program-delete", page)
        self.assertNotIn("/delete/", page)
        copy = programs.copy_template(self.me, "full_body")
        self.client.post(url("program_delete", copy.pk), {"remove_scheduled": "1"})
        self.assertFalse(Program.objects.filter(pk=copy.pk).exists())   # my copy goes ...
        self.assertEqual(programs.TEMPLATES, before)                     # ... the template stays
        self.assertContains(self.client.get(url("program_templates")), before["full_body"][0])

    def test_logged_workouts_are_kept_with_the_programs_names(self):
        p, day = self.make_program()
        w = programs.start_workout(self.me, day=day)
        programs.finish(w)
        renamed = programs.start_workout(self.me, day=day, name="Push day")
        r = self.client.post(url("program_delete", p.pk), {"remove_scheduled": "1"})
        self.assertRedirects(r, url("programs"), fetch_redirect_response=False)
        self.assertFalse(Program.objects.filter(pk=p.pk).exists())
        w.refresh_from_db()
        self.assertEqual((w.program_day_id, w.program_name, w.program_day_name), (None, "Split", "Upper A"))
        self.assertEqual(w.title, "Upper A (deleted program)")
        self.assertEqual(w.exercises.count(), 1)
        renamed.refresh_from_db()
        self.assertEqual(renamed.title, "Push day (deleted program)")
        self.assertContains(self.client.get(url("history")), "Upper A (deleted program)")
        self.assertContains(self.client.get(url("summary", w.pk)), "Upper A (deleted program)")
        msgs = [str(m) for m in r.wsgi_request._messages]
        self.assertEqual(msgs, ["Deleted Split. Your 2 logged workouts stay in History. Its upcoming Planner sessions are gone; past ones stay."])
        self.assertEqual(Workout(name="Quick log").title, "Quick log")             # nothing changes for the rest

    def test_remove_scheduled_ends_its_planner_tasks_and_keeps_history(self):
        p, day = self.make_program()
        item = self.day_item(day)
        past = self.today - timedelta(days=3)
        self.check_off(item, past)
        old = self.one_off(day, self.today - timedelta(days=2))
        self.check_off(old, self.today - timedelta(days=2))
        later = self.one_off(day, self.today + timedelta(days=5))
        self.client.post(url("program_delete", p.pk), {"remove_scheduled": "1"})
        item.refresh_from_db()
        self.assertEqual(item.recurrence_until, self.today - timedelta(days=1))   # today's wasn't done: gone too
        self.assertFalse(item.archived)
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=past).exists())
        self.assertEqual((item.source_app, item.source_id, item.source_url), ("", "", ""))   # no link to the program
        old.refresh_from_db()                                                      # the one-off's history stays ...
        self.assertTrue(ItemCompletion.objects.filter(item=old).exists())
        later.refresh_from_db()                                                    # ... its upcoming one leaves
        self.assertTrue(later.archived)
        self.assertFalse(ScheduledSession.objects.exists())
        self.assertEqual(services.upcoming_dates(item, self.today), [])

    def test_todays_checked_off_occurrence_stays(self):
        p, day = self.make_program()
        item = self.day_item(day)
        self.check_off(item, self.today)
        self.client.post(url("program_delete", p.pk), {"remove_scheduled": "1"})
        item.refresh_from_db()
        self.assertEqual(item.recurrence_until, self.today)
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=self.today).exists())

    def test_unchecked_keeps_its_planner_tasks_as_ordinary_tasks(self):
        p, day = self.make_program()
        item = self.day_item(day)
        until = item.recurrence_until
        self.check_off(item, self.today - timedelta(days=3))
        later = self.one_off(day, self.today + timedelta(days=5))
        r = self.client.post(url("program_delete", p.pk))                      # the checkbox off: not sent
        item.refresh_from_db()
        self.assertEqual((item.recurrence_until, item.archived, item.recurrence), (until, False, "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR,SA,SU"))
        self.assertEqual((item.source_app, item.source_id, item.source_url), ("", "", ""))
        self.assertTrue(ItemCompletion.objects.filter(item=item).exists())
        self.assertTrue(services.upcoming_dates(item, self.today + timedelta(days=1)))   # still repeating
        later.refresh_from_db()
        self.assertEqual((later.archived, later.source_app), (False, ""))
        self.assertIn("Its Planner tasks stay as ordinary tasks.", [str(m) for m in r.wsgi_request._messages][0])

    def test_shares_are_removed_and_nothing_goes_to_the_activity_feed(self):
        p, _day = self.make_program()
        ct = ContentType.objects.get_for_model(Program)
        ShareGrant.objects.create(content_type=ct, object_id=p.pk, user=self.wife, created_by=self.me)
        events = ActivityEvent.objects.count()
        self.client.post(url("program_delete", p.pk), {"remove_scheduled": "1"})
        self.assertFalse(ShareGrant.objects.filter(content_type=ct, object_id=p.pk).exists())
        self.assertEqual(ActivityEvent.objects.count(), events)                 # programs are private: nobody to tell


class ArchiveTests(ProgramDeleteBase):
    def test_archive_hides_it_pauses_it_and_restore_brings_it_back(self):
        p, day = self.make_program()
        item = self.day_item(day)
        r = self.client.post(url("program_archive", p.pk))
        self.assertRedirects(r, url("programs"), fetch_redirect_response=False)
        p.refresh_from_db()
        self.assertIsNotNone(p.archived_at)
        self.assertEqual((p.active, p.on_planner), (False, True))              # paused; the Planner choice kept
        item.refresh_from_db()
        self.assertEqual(item.recurrence_until, self.today)                     # like Pause: history stays
        self.assertEqual(item.source_app, "fitness")                            # nothing deleted or unlinked
        listing = self.client.get(url("programs")).content.decode()
        self.assertNotIn(f'id="program-{p.pk}"', listing)
        self.assertIn('id="fx-programs-archived">Archived (1)</a>', listing)
        archived = self.client.get(url("programs") + "?show=archived").content.decode()
        self.assertIn(f'id="program-{p.pk}"', archived)
        self.assertIn('aria-label="Restore Split">Restore</button>', archived)
        self.assertContains(self.client.get(url("program", p.pk)), "Archived - it's off your Programs list and paused.")
        self.client.post(url("program_archive", p.pk), {"restore": "1"})
        p.refresh_from_db()
        self.assertEqual((p.archived_at, p.active), (None, False))              # back on the list, still paused
        self.assertIn(f'id="program-{p.pk}"', self.client.get(url("programs")).content.decode())
        self.assertNotIn("fx-programs-archived", self.client.get(url("programs")).content.decode())
        self.client.post(url("program", p.pk), {"action": "resume"})
        p.refresh_from_db()
        self.assertTrue(p.active)

    def test_resuming_an_archived_program_restores_it(self):
        p, _day = self.make_program(planner=False)
        programs.archive(p)
        programs.set_active(p, True)
        p.refresh_from_db()
        self.assertEqual((p.active, p.archived_at), (True, None))

    def test_archived_programs_are_off_today(self):
        p, day = self.make_program(planner=False)
        self.assertEqual([d for d, _w in stats.todays(self.me)], [day])
        programs.archive(p)
        self.assertEqual(stats.todays(self.me), [])


class BotDeleteTests(ProgramDeleteBase):
    def call(self, method, path, raw, body=None):
        headers = {"HTTP_AUTHORIZATION": f"Bearer {raw}"}
        if method == "GET":
            return Client().get(API + path, **headers)
        return Client().generic(method, API + path, data=json.dumps(body) if body is not None else "",
                                content_type="application/json", **headers)

    def test_delete_removing_scheduled_by_default(self):
        p, day = self.make_program()
        w = programs.start_workout(self.me, day=day)
        item = self.day_item(day)
        raw = BotToken.issue(self.me, "Coach", ["fitness:read", "fitness:write"])[1]
        r = self.call("DELETE", f"fitness/programs/{p.pk}", raw)
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual(r.json(), {"deleted": p.pk, "name": "Split", "workouts_kept": 1, "planner_ended": 1, "planner_kept": 0})
        self.assertFalse(Program.objects.filter(pk=p.pk).exists())
        w.refresh_from_db()
        self.assertEqual(w.title, "Upper A (deleted program)")
        item.refresh_from_db()
        self.assertEqual((item.recurrence_until, item.source_app), (self.today - timedelta(days=1), ""))

    def test_delete_keeping_scheduled(self):
        p, day = self.make_program()
        item = self.day_item(day)
        until = item.recurrence_until
        raw = BotToken.issue(self.me, "Coach", ["fitness:write"])[1]
        r = self.call("DELETE", f"fitness/programs/{p.pk}", raw, {"remove_scheduled": False})
        self.assertEqual(r.json(), {"deleted": p.pk, "name": "Split", "workouts_kept": 0, "planner_ended": 0, "planner_kept": 1})
        item.refresh_from_db()
        self.assertEqual((item.recurrence_until, item.archived, item.source_app), (until, False, ""))
        q, day2 = self.make_program(name="Other")                                # the query-string form too
        item2 = self.day_item(day2)
        self.assertEqual(self.call("DELETE", f"fitness/programs/{q.pk}?remove_scheduled=false", raw).json()["planner_kept"], 1)
        item2.refresh_from_db()
        self.assertEqual(item2.recurrence_until, until)

    def test_owner_only_scope_and_bad_values(self):
        p, _day = self.make_program()
        mine_read = BotToken.issue(self.me, "Reader", ["fitness:read"])[1]
        self.assertEqual(self.call("DELETE", f"fitness/programs/{p.pk}", mine_read).status_code, 403)
        theirs = BotToken.issue(self.wife, "Theirs", ["fitness:write"])[1]
        self.assertEqual(self.call("DELETE", f"fitness/programs/{p.pk}", theirs).status_code, 404)
        mine = BotToken.issue(self.me, "Coach", ["fitness:read", "fitness:write"])[1]
        self.assertEqual(self.call("DELETE", f"fitness/programs/{p.pk}", mine, {"remove_scheduled": "maybe"}).status_code, 400)
        self.assertEqual(self.call("GET", f"fitness/programs/{p.pk}", mine).status_code, 405)
        self.assertTrue(Program.objects.filter(pk=p.pk).exists())
        listed = self.call("GET", "fitness/programs", mine).json()["programs"]
        self.assertEqual(listed[0]["archived"], False)
        docs = Client().get(API + "docs", HTTP_AUTHORIZATION=f"Bearer {mine}").content.decode()
        self.assertIn("DELETE fitness/programs/<id>", docs)
        self.assertIn("remove_scheduled", docs)
