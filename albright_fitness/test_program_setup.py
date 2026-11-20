"""Programs made easy: start date, a remembered Planner choice and time, pause / resume, drafts that stay off,
editing a day's exercises (programs.py), and the exercise type-ahead (program_views)."""
from datetime import time, timedelta

from albright_planner.models import ScheduleItem

from . import programs, stats
from .models import Exercise, Program
from .tests import Base, url


class ProgramBackendTests(Base):
    def test_draft_stays_off_until_finished_then_starts_on_its_date(self):
        p = programs.copy_template(self.me, "full_body", active=False)
        p.days.update(weekdays=[0, 1, 2, 3, 4, 5, 6])
        self.assertTrue(p.is_draft)
        self.assertEqual(stats.todays(self.me), [])
        programs.finish_setup(p, self.today + timedelta(days=3))
        self.assertFalse(p.is_draft)
        self.assertEqual(stats.todays(self.me), [])                       # not started yet
        self.assertEqual(len(stats.todays(self.me, self.today + timedelta(days=3))), 2)
        self.assertFalse(ScheduleItem.objects.filter(owner=self.me, source_app="fitness").exists())

    def test_planner_with_a_time_and_pause_resume_brings_it_back(self):
        p = programs.copy_template(self.me, "full_body", active=False)
        placed = programs.finish_setup(p, self.today, weeks=8, on_planner=True, planner_time=time(6, 30))
        self.assertEqual(placed, 2)
        item = ScheduleItem.objects.filter(owner=self.me, source_app="fitness").first()
        self.assertEqual(item.start_time, time(6, 30))
        programs.set_active(p, False)
        p.refresh_from_db()
        self.assertEqual((p.active, p.scheduled_from, p.on_planner), (False, None, True))   # remembered
        programs.set_active(p, True)
        p.refresh_from_db()
        self.assertIsNotNone(p.scheduled_from)
        item.refresh_from_db()
        self.assertGreater(item.recurrence_until, self.today)

    def test_planner_switch_off_forgets(self):
        p = programs.copy_template(self.me, "full_body")
        programs.set_planner(p, True)
        self.assertTrue(p.on_planner)
        programs.set_planner(p, False)
        p.refresh_from_db()
        self.assertEqual((p.on_planner, p.scheduled_from), (False, None))
        programs.set_active(p, False)
        programs.set_active(p, True)
        self.assertIsNone(Program.objects.get(pk=p.pk).scheduled_from)

    def test_add_target_and_reorder(self):
        p = programs.copy_template(self.me, "full_body")
        day = p.days.get(name="Full body B")
        run = programs.add_exercise(day, self.ex("Running"))
        self.assertEqual(run.target_text, "20 min")
        bench = programs.add_exercise(day, self.ex("Barbell Bench Press"))
        self.assertEqual(bench.target_text, "3 × 10")
        programs.set_target(bench, sets=5, reps=5)
        programs.move_exercise(bench, -1)
        names = [pe.exercise.name for pe in day.exercises.all()]
        self.assertEqual(names[-2:], ["Barbell Bench Press", "Running"])
        self.assertEqual([pe.order for pe in day.exercises.all()], list(range(len(names))))
        self.assertEqual(day.exercises.get(pk=bench.pk).target_text, "5 × 5")
        programs.move_exercise(day.exercises.first(), -1)                # already first: stays

    def test_existing_programs_count_from_when_they_were_made(self):
        p = Program.objects.create(owner=self.me, name="Old")
        p.days.create(name="Any", weekdays=[0, 1, 2, 3, 4, 5, 6])
        self.assertEqual(len(stats.todays(self.me)), 1)


class ExerciseTypeAheadTests(Base):
    def test_search_finds_library_and_mine_but_not_others(self):
        Exercise.objects.create(owner=self.friend, name="Kim's Secret Squat")
        Exercise.objects.create(owner=self.me, name="My Sandbag Squat")
        data = self.client.get(url("exercise_search") + "?q=squat").json()
        names = [r["name"] for r in data["results"]]
        self.assertIn("Barbell Back Squat", names)
        self.assertIn("My Sandbag Squat", names)
        self.assertNotIn("Kim's Secret Squat", names)
        self.assertEqual(data["create"], "squat")
        exact = self.client.get(url("exercise_search") + "?q=barbell back squat").json()
        self.assertEqual((exact["results"][0]["name"], exact["create"]), ("Barbell Back Squat", None))
        self.assertEqual(self.client.get(url("exercise_search")).json(), {"results": [], "create": None})

    def test_create_my_own_inline_once(self):
        r = self.client.post(url("exercise_create"), {"name": "  Sled  push ", "kind": "cardio"})
        self.assertEqual(r.status_code, 201)
        ex = Exercise.objects.get(owner=self.me, name="Sled push")
        self.assertEqual(ex.kind, "cardio")
        again = self.client.post(url("exercise_create"), {"name": "sled PUSH"})
        self.assertEqual((again.status_code, again.json()["exercise"]["id"]), (200, ex.pk))
        self.assertEqual(self.client.post(url("exercise_create"), {"name": " "}).status_code, 400)
        self.assertEqual(self.client.post(url("exercise_create"), {"name": "Barbell Row"}).json()["created"], False)


class GuidedSetupTests(Base):
    def test_three_steps_from_nothing_to_on_the_planner(self):
        r = self.client.post(url("program_new"), {"name": "Summer", "rows": "1", "name_0": "Upper", "weekdays_0": ["0", "3"],
                                                 "action": "more"})
        self.assertContains(r, 'name="name_1"')                         # "+ Add another workout" keeps what's typed
        self.assertContains(r, 'value="Upper"')
        self.assertFalse(Program.objects.exists())
        r = self.client.post(url("program_new"), {"name": "Summer", "rows": "2", "name_0": "Upper", "weekdays_0": ["0", "3"],
                                                 "name_1": "Lower", "weekdays_1": ["1", "4"], "action": "next"})
        p = Program.objects.get(owner=self.me)
        self.assertRedirects(r, url("program_setup_exercises", p.pk))
        self.assertTrue(p.is_draft)
        upper, lower = p.days.all()
        self.assertEqual((upper.weekdays, lower.weekdays), ([0, 3], [1, 4]))
        page = self.client.get(url("program_setup_exercises", p.pk) + "?q=bench").content.decode()
        bench = self.ex("Barbell Bench Press")
        self.assertIn(f'value="add:{bench.pk}"', page)
        self.client.post(url("program_setup_exercises", p.pk), {"day": upper.pk, "action": f"add:{bench.pk}"})
        self.client.post(url("program_setup_exercises", p.pk), {"day": upper.pk, "action": "create", "new_name": "Ring dips",
                                                                "new_kind": "bodyweight"})
        pe = upper.exercises.get(exercise=bench)
        r = self.client.post(url("program_setup_exercises", p.pk), {"day": upper.pk, f"sets_{pe.pk}": "4", f"reps_{pe.pk}": "6",
                                                                    "action": "next"})
        self.assertRedirects(r, url("program_setup_exercises", p.pk) + f"?day={lower.pk}")
        self.assertEqual(upper.exercises.get(pk=pe.pk).target_text, "4 × 6")
        self.assertEqual([x.exercise.name for x in upper.exercises.all()], ["Barbell Bench Press", "Ring dips"])
        self.client.post(url("program_setup_exercises", p.pk), {"day": upper.pk, "action": f"down:{pe.pk}"})
        self.assertEqual(upper.exercises.first().exercise.name, "Ring dips")
        self.assertEqual(stats.todays(self.me, self.today + timedelta(days=7 - self.today.weekday())), [])  # still a draft
        r = self.client.post(url("program_setup_schedule", p.pk), {"start_date": self.today.isoformat(), "weeks": "8",
                                                                   "planner": "1", "time": "06:30"})
        self.assertRedirects(r, url("program", p.pk))
        p.refresh_from_db()
        self.assertEqual((p.active, p.weeks, p.on_planner, p.planner_time), (True, 8, True, time(6, 30)))
        self.assertEqual(ScheduleItem.objects.filter(owner=self.me, source_app="fitness").count(), 2)
        page = self.client.get(url("program", p.pk)).content.decode()
        self.assertIn('id="fx-program-start"', page)
        self.assertIn("6:30 AM", page)
        self.assertNotIn("Schedule this day", page)                        # one system on the program page
        self.assertNotIn("Schedule this program", page)

    def test_step_one_needs_a_name_and_days(self):
        r = self.client.post(url("program_new"), {"name": "", "rows": "1", "name_0": "Upper", "action": "next"})
        self.assertContains(r, "Give the program a name.")
        self.assertContains(r, "Pick the days for Upper.")
        self.assertFalse(Program.objects.exists())

    def test_editing_renames_removes_and_the_planner_follows(self):
        p = programs.copy_template(self.me, "full_body")
        programs.schedule(p)
        a, b = p.days.all()
        self.client.post(url("program_setup", p.pk), {"name": "Mine", "rows": "2", "id_0": a.pk, "name_0": "A", "weekdays_0": ["0"],
                                                     "id_1": b.pk, "name_1": b.name, "weekdays_1": ["2"], "remove_1": "1",
                                                     "action": "next"})
        p.refresh_from_db()
        self.assertEqual((p.name, [d.name for d in p.days.all()]), ("Mine", ["A"]))
        item = ScheduleItem.objects.get(owner=self.me, source_app="fitness", source_id=f"day-{a.pk}", archived=False)
        self.assertEqual(item.recurrence, "FREQ=WEEKLY;BYDAY=MO")

    def test_template_link_copies_a_draft_into_step_two(self):
        self.assertContains(self.client.get(url("program_new")), 'id="fx-from-template"')
        page = self.client.get(url("program_templates") + "?days=3").content.decode()
        self.assertIn("Full body 3x/week", page)
        self.assertNotIn("Push/Pull/Legs", page)
        r = self.client.post(url("program_templates"), {"template": "full_body"})
        p = Program.objects.get(owner=self.me)
        self.assertRedirects(r, url("program_setup_exercises", p.pk))
        self.assertTrue(p.is_draft)
        self.assertEqual(stats.todays(self.me), [])

    def test_program_page_pause_resume_and_planner_switch(self):
        p = programs.copy_template(self.me, "full_body")
        self.client.post(url("program", p.pk), {"action": "planner", "on": "1"})
        self.assertTrue(Program.objects.get(pk=p.pk).on_planner)
        self.client.post(url("program", p.pk), {"action": "pause"})
        p.refresh_from_db()
        self.assertEqual((p.active, p.on_planner, p.scheduled_from), (False, True, None))
        self.assertContains(self.client.get(url("program", p.pk)), "Resume")
        self.client.post(url("program", p.pk), {"action": "resume"})
        self.assertIsNotNone(Program.objects.get(pk=p.pk).scheduled_from)
        self.client.post(url("program", p.pk), {"action": "planner", "on": "0"})
        self.assertFalse(Program.objects.get(pk=p.pk).on_planner)

    def test_list_shows_drafts_and_someone_elses_program_is_a_404(self):
        draft = programs.copy_template(self.me, "running", active=False)
        page = self.client.get(url("programs")).content.decode()
        self.assertIn("Draft", page)
        self.assertIn(url("program_setup", draft.pk), page)
        theirs = programs.copy_template(self.friend, "running")
        for name in ("program", "program_setup", "program_setup_exercises", "program_setup_schedule"):
            self.assertEqual(self.client.get(url(name, theirs.pk)).status_code, 302, name)
        self.assertEqual(self.client.post(url("program", theirs.pk), {"action": "pause"}).status_code, 404)
