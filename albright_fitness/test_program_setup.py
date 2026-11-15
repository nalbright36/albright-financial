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
