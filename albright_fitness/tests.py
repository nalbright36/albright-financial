"""Albright Fitness: the seeded library, logging (+ Set copies, last time and the suggested target, the quick text
log), personal records (weight, Epley 1RM, reps, cardio), program templates, the Planner link (recurring private
tasks with exercise checklists; finishing a workout checks its occurrence off; turning a program off ends them),
privacy and sharing, the dashboard and launcher line, and the registry entry with its yellow accent."""
import pathlib
import re
from datetime import date, timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth.models import User
from django.contrib.staticfiles import finders
from django.core.cache import cache
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from albright_labs import apps_registry, launcher, tiles
from albright_labs.testing import with_apps
from albright_planner import recurrence
from albright_planner.models import ItemCompletion, ScheduleItem

from . import programs, seed, stats, training
from .models import (Exercise, PersonalRecord, Program, ProgramDay, ProgramExercise, Workout, WorkoutExercise, WorkoutSet,
                     WorkoutShare)

PASSWORD = "pw-not-real-12345"
D = Decimal


def url(name, *args):
    return reverse(f"albright_fitness:{name}", args=args)


def static(path):
    return pathlib.Path(finders.find(path)).read_text(encoding="utf-8")


def contrast(a, b):
    def lum(h):
        c = [int(h.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]
    high, low = sorted((lum(a), lum(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


class Base(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch("albright_trading_app.views.get_market_outlook", return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.me = with_apps(User.objects.create_user(username="nate", password=PASSWORD, first_name="Nate"))
        self.friend = with_apps(User.objects.create_user(username="kim", password=PASSWORD, first_name="Kim"), ["fitness"])
        self.client.force_login(self.me)
        self.today = timezone.localdate()

    def ex(self, name):
        return Exercise.objects.get(owner__isnull=True, name=name)

    def workout(self, owner=None, day=None, finished=True, name="", sets=(), exercise="Barbell Back Squat", target_reps=None):
        """A workout with one exercise and these sets: [(weight, reps)] or [{"...": ...}] - done, working."""
        w = Workout.objects.create(owner=owner or self.me, date=day or self.today, name=name,
                                   finished_at=timezone.now() if finished else None)
        we = WorkoutExercise.objects.create(workout=w, exercise=self.ex(exercise), target_reps=target_reps)
        for n, s in enumerate(sets):
            fields = s if isinstance(s, dict) else {"weight": D(str(s[0])), "reps": s[1]}
            WorkoutSet.objects.create(workout_exercise=we, order=n, done=fields.pop("done", True), **fields)
        return w

    def as_user(self, user):
        c = Client()
        c.force_login(user)
        return c


# ---------------------------------------------------------------------------
# Registry, colors, pages
# ---------------------------------------------------------------------------

class RegistryTests(Base):
    def test_registry_entry_and_yellow_accent(self):
        app = apps_registry.BY_KEY["fitness"]
        self.assertEqual((app.label, app.short_label, app.brand, app.accent, app.color),
                         ("Fitness", "Fitness", "Albright Fitness", "fitness", "#FACC15"))
        css = static("style.css")
        dark = css[css.index('\nbody[data-app="fitness"] {'):]
        dark = dark[:dark.index("}")]
        for part in ("--accent: #FACC15;", "--accent-solid: #FACC15;", "--accent-contrast: #1C1917;",
                     "--accent-soft: rgba(250, 204, 21, 0.15);", "--accent-rgb: 250, 204, 21;"):
            self.assertIn(part, dark)
        light = css[css.index('[data-theme="light"] body[data-app="fitness"] {'):]
        light = light[:light.index("}")]
        for part in ("--accent-solid: #CA8A04;", "--accent-soft: rgba(202, 138, 4, 0.15);", "--accent: #9C5F06;"):
            self.assertIn(part, light)
        self.assertIn("--app-fitness: #FACC15;", css)
        self.assertIn("--app-fitness: #CA8A04;", css)

    def test_wcag_aa_in_both_modes(self):
        self.assertGreaterEqual(contrast("#FACC15", "#12151A"), 4.5)      # dark: text on the surface
        self.assertGreaterEqual(contrast("#9C5F06", "#FFFFFF"), 4.5)      # light: text on cards
        self.assertGreaterEqual(contrast("#9C5F06", "#F3F4F6"), 4.5)      # light: text on the page
        self.assertGreaterEqual(contrast("#1C1917", "#FACC15"), 4.5)      # dark text on the yellow buttons
        self.assertGreaterEqual(contrast("#1C1917", "#CA8A04"), 4.5)
        self.assertGreaterEqual(contrast("#1C1917", "#FDE047"), 4.5)      # hover
        self.assertGreaterEqual(contrast("#1C1917", "#EAB308"), 4.5)

    def test_warnings_moved_toward_orange(self):
        css = static("style.css")
        self.assertIn("--amber: #F59E0B;", css)
        self.assertIn("--amber: #B45309;", css)
        self.assertNotIn("--amber: #FACC15;", css)                      # never Fitness's yellow
        self.assertGreaterEqual(contrast("#B45309", "#FFFFFF"), 4.5)
        self.assertGreaterEqual(contrast("#F59E0B", "#12151A"), 4.5)

    def test_new_users_get_no_access(self):
        newbie = User.objects.create_user(username="new", password=PASSWORD)
        self.assertEqual(self.as_user(newbie).get(url("dashboard")).status_code, 404)
        self.assertEqual(self.as_user(with_apps(newbie, ["notes"])).get(url("dashboard")).status_code, 404)

    def test_stylesheet_script_and_back_chip_on_every_page(self):
        w = self.workout(sets=[(225, 5)])
        program = programs.copy_template(self.me, "full_body")
        paths = [url("dashboard"), url("exercises"), url("exercise", self.ex("Deadlift").pk), url("start"), url("workout", w.pk),
                 url("summary", w.pk), url("history"), url("programs"), url("program", program.pk)]
        for path in paths:
            page = self.client.get(path).content.decode()
            self.assertRegex(page, r'<link rel="stylesheet" href="/static/albright_fitness/fitness\.css\?v=\d+">', path)
            self.assertRegex(page, r'<script src="/static/albright_fitness/fitness\.js\?v=\d+"></script>', path)
            self.assertIn('<body data-app="fitness">', page, path)
            self.assertEqual(page.count('id="nav-back"'), 1, path)
        page = self.client.get(url("dashboard")).content.decode()
        self.assertRegex(page, r'data-app-key="fitness" data-app-target="fitness" title="Albright Fitness"')
        self.assertIn('<path d="M6.5 6.5v11"/>', page)                    # the dumbbell


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------

class LibraryTests(Base):
    def test_seeded_exercises(self):
        builtin = Exercise.objects.filter(owner__isnull=True)
        self.assertGreaterEqual(builtin.count(), 80)
        self.assertEqual(builtin.count(), len(seed.EXERCISES))
        for name in ("Barbell Back Squat", "Dumbbell Curl", "Leg Press", "Pull-Up", "Running", "Cycling", "Rowing Machine"):
            self.assertTrue(builtin.filter(name=name).exists(), name)
        self.assertEqual(set(builtin.values_list("kind", flat=True)), {"strength", "cardio", "bodyweight", "mobility"})
        seed.seed(Exercise)                                               # again: nothing doubled
        self.assertEqual(Exercise.objects.filter(owner__isnull=True).count(), len(seed.EXERCISES))

    def test_search_filters_and_my_own(self):
        page = self.client.get(url("exercises") + "?q=curl").content.decode()
        self.assertIn("Hammer Curl", page)
        self.assertNotIn("Deadlift</span>", page)
        page = self.client.get(url("exercises") + "?muscle=calves&equipment=machine").content.decode()
        self.assertIn("Seated Calf Raise", page)
        self.assertNotIn("Dumbbell Calf Raise", page)
        self.client.post(url("exercises"), {"name": "Landmine Press", "kind": "strength", "equipment": "barbell",
                                            "primary": ["shoulders"]})
        mine = Exercise.objects.get(name="Landmine Press")
        self.assertEqual((mine.owner, mine.primary_muscles), (self.me, ["shoulders"]))
        self.assertEqual(self.as_user(self.friend).get(url("exercise", mine.pk)).status_code, 404)   # mine only


# ---------------------------------------------------------------------------
# Logging: + Set, last time, suggestions, quick log
# ---------------------------------------------------------------------------

class LoggingTests(Base):
    def test_plus_set_copies_the_previous_set(self):
        w = self.workout(finished=False, sets=[{"weight": D("185"), "reps": 8, "unit": "lb", "rpe": D("8"), "done": True}])
        we = w.exercises.get()
        response = self.client.post(url("add_set", we.pk), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        new = WorkoutSet.objects.get(pk=response.json()["id"])
        self.assertEqual((new.weight, new.reps, new.unit, new.order, new.done, new.rpe), (D("185.00"), 8, "lb", 1, False, None))
        self.assertIn(f'data-set="{new.pk}"', response.json()["html"])

    def test_updating_and_checking_a_set(self):
        w = self.workout(finished=False, sets=[{"weight": D("100"), "reps": 5, "done": False}])
        s = WorkoutSet.objects.get()
        self.client.post(url("update_set", s.pk), {"weight": "102.5", "reps": "6", "rpe": "8.5", "done": "1"},
                         HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        s.refresh_from_db()
        self.assertEqual((s.weight, s.reps, s.rpe, s.done), (D("102.50"), 6, D("8.5"), True))
        self.client.post(url("update_set", s.pk), {"toggle_done": "1"})          # without JS: the box toggles
        s.refresh_from_db()
        self.assertFalse(s.done)
        self.assertEqual(self.as_user(self.friend).post(url("update_set", s.pk), {"reps": "1"}).status_code, 404)
        self.assertTrue(w.pk)

    def test_last_time_and_progression(self):
        squat, bench = self.ex("Barbell Back Squat"), self.ex("Barbell Bench Press")
        self.workout(day=self.today - timedelta(days=3), sets=[(225, 5), (225, 5), (225, 5)])
        tip = training.suggest(self.me, squat)
        self.assertEqual((tip.reps, tip.weight, tip.increase), (5, D("230"), True))     # lower body: +5 lb
        self.assertEqual([(s.weight, s.reps) for s in tip.last], [(D("225.00"), 5)] * 3)
        self.workout(day=self.today - timedelta(days=2), exercise="Barbell Bench Press", sets=[(185, 8), (185, 8), (185, 8)])
        self.assertEqual(training.suggest(self.me, bench).weight, D("187.5"))           # upper body: +2.5 lb
        self.workout(day=self.today - timedelta(days=1), sets=[(230, 5), (230, 5), (230, 4)])
        tip = training.suggest(self.me, squat)
        self.assertEqual((tip.weight, tip.increase), (D("230.00"), False))              # missed a rep: repeat
        self.assertIn("repeat", tip.text)
        self.workout(day=self.today, sets=[(240, 8), (240, 8)], target_reps=10)          # a program's 10 not reached
        self.assertFalse(training.suggest(self.me, squat).increase)

    def test_starting_a_day_prefills_targets_and_progression(self):
        self.workout(day=self.today - timedelta(days=7), sets=[(225, 5)] * 3)
        program = programs.copy_template(self.me, "five_by_five")
        day = program.days.get(name="Workout A")
        response = self.client.post(url("start"), {"day": day.pk})
        w = Workout.objects.filter(finished_at__isnull=True).get()
        self.assertRedirects(response, url("workout", w.pk), fetch_redirect_response=False)
        squat = w.exercises.get(exercise__name="Barbell Back Squat")
        self.assertEqual(squat.sets.count(), 5)                                          # 5 x 5
        self.assertEqual({(s.reps, s.weight) for s in squat.sets.all()}, {(5, D("230.00"))})
        page = self.client.get(url("workout", w.pk)).content.decode()
        self.assertIn("Last time:", page)
        self.assertIn("Try 5 × 5 @ 230 lb (+5)", page.replace("&times;", "×"))

    def test_quick_text_log(self):
        parsed = training.parse_quick("squat 225x5x3, bench 185x8,8,7, run 3.1mi 28:30, pullups 10,8,6, deadlift 140kgx5", self.me)
        self.assertEqual([p.exercise.name for p in parsed], ["Barbell Back Squat", "Barbell Bench Press", "Running", "Pull-Up", "Deadlift"])
        self.assertEqual(parsed[0].sets, [{"reps": 5, "weight": D("225"), "unit": "lb"}] * 3)
        self.assertEqual([s["reps"] for s in parsed[1].sets], [8, 8, 7])
        self.assertEqual(parsed[2].sets, [{"distance": D("3.1"), "distance_unit": "mi", "duration_seconds": 1710}])
        self.assertEqual(parsed[3].sets, [{"reps": 10}, {"reps": 8}, {"reps": 6}])
        self.assertEqual(parsed[4].sets[0]["unit"], "kg")
        bad = training.parse_quick("zorbing 5x5", self.me)
        self.assertIn("couldn't find", bad[0].problem)
        self.client.post(url("quick"), {"text": "squat 225x5x3, bench 185x8,8,7, run 3.1mi 28:30"})
        w = Workout.objects.get(name="Quick log")
        self.assertTrue(w.finished)
        self.assertEqual(WorkoutSet.objects.filter(workout_exercise__workout=w, done=True).count(), 7)


# ---------------------------------------------------------------------------
# Personal records
# ---------------------------------------------------------------------------

class RecordTests(Base):
    def test_epley(self):
        self.assertEqual(training.epley(D("225"), 5), D("262.50"))
        self.assertEqual(training.epley(D("300"), 1), D("300"))

    def test_first_session_is_the_baseline(self):
        w = self.workout(sets=[(225, 5)])
        self.assertEqual(training.detect_records(w), [])

    def test_weight_1rm_and_reps_records(self):
        self.workout(day=self.today - timedelta(days=7), sets=[(225, 5), (205, 8)])
        w = self.workout(sets=[(235, 3), (205, 10)])
        found = {r.kind: r for r in programs.finish(w)}
        self.assertEqual((found["weight"].value, found["weight"].previous), (D("235.00"), D("225.00")))
        self.assertEqual(found["e1rm"].value, D("273.33"))                  # 205 x 10 -> 273.33 beats 262.50
        self.assertEqual((found["reps"].value, found["reps"].at_weight, found["reps"].previous), (D("10"), D("205.00"), D("8")))
        page = self.client.get(url("summary", w.pk)).content.decode()
        self.assertIn("3 new personal records!", page)
        self.assertIn("Heaviest weight: 235 lb (was 225 lb)", page)

    def test_kilograms_compare_in_pounds(self):
        self.workout(day=self.today - timedelta(days=7), sets=[(225, 5)])
        w = self.workout(sets=[{"weight": D("100"), "reps": 5, "unit": "kg"}])       # 220.46 lb: not a record
        self.assertNotIn("weight", {r.kind for r in programs.finish(w)})

    def test_cardio_records(self):
        self.workout(day=self.today - timedelta(days=7), exercise="Running",
                     sets=[{"distance": D("3.0"), "duration_seconds": 1800}])
        w = self.workout(exercise="Running", sets=[{"distance": D("3.1"), "duration_seconds": 1710}])
        found = {r.kind: r for r in programs.finish(w)}
        self.assertEqual(found["distance"].value, D("3.10"))
        self.assertEqual(found["pace"].value, D("551.61"))                  # 9:11 a mile, faster than 10:00
        self.assertNotIn("duration", found)                                 # shorter than last time
        self.assertEqual(training.record_text(found["pace"]), "Fastest pace: 9:11/mi (was 10:00/mi)")


# ---------------------------------------------------------------------------
# Programs and the Planner
# ---------------------------------------------------------------------------

class ProgramTests(Base):
    def test_templates_copy(self):
        self.assertEqual(set(programs.TEMPLATES), {"full_body", "upper_lower", "ppl", "five_by_five", "running"})
        for key in programs.TEMPLATES:
            self.client.post(url("programs"), {"template": key})
        names = list(Program.objects.filter(owner=self.me).order_by("name").values_list("name", flat=True))
        self.assertEqual(names, ["Beginner 5x5", "Full body 3x/week", "Push/Pull/Legs 6x/week", "Running 3x/week",
                                 "Upper/Lower 4x/week"])
        ppl = Program.objects.get(name="Push/Pull/Legs 6x/week")
        self.assertEqual(sorted(sum((d.weekdays for d in ppl.days.all()), [])), [0, 1, 2, 3, 4, 5])
        run = Program.objects.get(name="Running 3x/week").days.get(name="Long run").exercises.get()
        self.assertEqual((run.exercise.name, run.target_text), ("Running", "5 mi"))
        # Every template exercise exists in the library (nothing silently dropped).
        for _name, _weeks, days in programs.TEMPLATES.values():
            for _day, _wd, rows in days:
                for ex, *_ in rows:
                    self.assertTrue(Exercise.objects.filter(owner__isnull=True, name=ex).exists(), ex)

    def test_schedule_creates_recurring_private_tasks_with_checklists(self):
        program = programs.copy_template(self.me, "upper_lower")
        self.client.post(url("program", program.pk), {"action": "schedule"})
        items = ScheduleItem.objects.filter(owner=self.me, source_app="fitness").order_by("title")
        self.assertEqual(items.count(), 4)
        upper = items.get(source_id=f"day-{program.days.get(name='Upper A').pk}")
        self.assertEqual((upper.recurrence, upper.kind, upper.checkable), ("FREQ=WEEKLY;BYDAY=MO", "task", True))
        self.assertEqual(upper.date.weekday(), 0)
        self.assertIsNotNone(upper.recurrence_until)                        # not one-at-a-time: ends after its weeks
        self.assertFalse(upper.shares.exists())                             # private
        self.assertEqual([r.text for r in upper.checklist.all()][:2], ["Barbell Bench Press - 4 × 6", "Barbell Row - 4 × 6"])
        self.assertTrue(upper.source_url.startswith("/fitness/start/?day="))
        # Again: updated in place, never doubled.
        programs.schedule(program)
        self.assertEqual(ScheduleItem.objects.filter(owner=self.me, source_app="fitness").count(), 4)

    def test_finishing_a_workout_from_the_planner_checks_off_that_occurrence(self):
        program = programs.copy_template(self.me, "full_body")
        programs.schedule(program)
        day = program.days.get(name="Full body A")
        item = ScheduleItem.objects.get(source_id=f"day-{day.pk}")
        when = recurrence.first_after(item, self.today - timedelta(days=1))
        self.client.post(url("start") + f"?day={day.pk}&date={when}", {"day": day.pk, "date": when.isoformat()})
        w = Workout.objects.get(finished_at__isnull=True)
        self.assertEqual((w.planner_item, w.planner_date), (item, when))
        self.assertFalse(ItemCompletion.objects.filter(item=item).exists())
        self.client.post(url("finish", w.pk))
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=when).exists())

    def test_the_planner_asks_to_log_when_checking_a_workout_task(self):
        program = programs.copy_template(self.me, "full_body")
        programs.schedule(program, start=self.today)
        day = program.days.get(name="Full body A")
        item = ScheduleItem.objects.get(source_id=f"day-{day.pk}")
        page = self.client.get(reverse("albright_planner:week") + f"?date={item.date}").content.decode()
        self.assertIn(f'data-log-url="/fitness/start/?day={day.pk}&amp;date={item.date}"', page)
        js = static("albright_planner/planner.js")
        self.assertIn("Log the workout?", js)
        self.assertIn('if (logChoice === "log") { window.location.href = logUrl; return null; }', js)

    def test_turning_a_program_off_ends_its_planner_items_but_keeps_history(self):
        program = programs.copy_template(self.me, "full_body")
        programs.schedule(program, start=self.today - timedelta(days=14))
        item = ScheduleItem.objects.filter(source_app="fitness").first()
        past = recurrence.first_after(item, item.date - timedelta(days=1))
        ItemCompletion.objects.create(item=item, occurrence_date=past)
        self.client.post(url("program", program.pk), {"action": "active", "active": "0"})
        item.refresh_from_db()
        self.assertEqual(item.recurrence_until, self.today)
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=past).exists())
        program.refresh_from_db()
        self.assertEqual((program.active, program.scheduled_from), (False, None))

    def test_planner_writes_go_through_its_service_helpers(self):
        source = pathlib.Path(programs.__file__).read_text(encoding="utf-8")
        self.assertNotIn("ScheduleItem.objects", source)
        self.assertNotIn("ItemCompletion", source)
        self.assertIn("services.upsert_weekly_source_item(", source)
        self.assertIn("services.complete_occurrence(", source)
        self.assertIn("services.end_source_items(", source)


# ---------------------------------------------------------------------------
# Privacy and sharing
# ---------------------------------------------------------------------------

class PrivacyTests(Base):
    def test_private_by_default_and_shared_view_only(self):
        w = self.workout(sets=[(225, 5)], name="Leg day")
        kim = self.as_user(self.friend)
        for path in (url("workout", w.pk), url("summary", w.pk)):
            self.assertEqual(kim.get(path).status_code, 404)
        self.assertNotIn("Leg day", kim.get(url("history")).content.decode())
        self.client.post(url("share", w.pk), {"user": self.friend.pk})
        self.assertTrue(WorkoutShare.objects.filter(workout=w, user=self.friend).exists())
        page = kim.get(url("workout", w.pk)).content.decode()
        self.assertIn("Nate's workout (view only)", page.replace("&#x27;", "'"))
        self.assertNotIn("data-set-form", page)                              # nothing to change
        s = WorkoutSet.objects.get()
        self.assertEqual(kim.post(url("update_set", s.pk), {"reps": "1"}).status_code, 404)
        self.assertEqual(kim.post(url("finish", w.pk)).status_code, 404)
        self.assertEqual(kim.post(url("delete_workout", w.pk)).status_code, 404)
        self.assertIn("Leg day", kim.get(url("history")).content.decode())
        self.client.post(url("share", w.pk), {"user": self.friend.pk, "action": "remove"})
        self.assertEqual(kim.get(url("workout", w.pk)).status_code, 404)

    def test_programs_and_records_are_never_shared(self):
        program = programs.copy_template(self.me, "full_body")
        kim = self.as_user(self.friend)
        self.assertEqual(kim.get(url("program", program.pk)).status_code, 404)
        self.assertEqual(kim.post(url("start"), {"day": program.days.first().pk}).status_code, 404)
        self.workout(day=self.today - timedelta(days=7), sets=[(225, 5)])
        programs.finish(self.workout(sets=[(235, 5)]))
        self.assertNotIn("Barbell Back Squat", kim.get(url("dashboard")).content.decode().split('id="fx-prs-h"', 1)[1])

    def test_only_people_with_fitness_access_can_be_shared_with(self):
        w = self.workout(sets=[(225, 5)])
        outsider = with_apps(User.objects.create_user(username="pat", password=PASSWORD), ["notes"])
        self.assertEqual(self.client.post(url("share", w.pk), {"user": outsider.pk}).status_code, 404)


# ---------------------------------------------------------------------------
# Dashboard and launcher
# ---------------------------------------------------------------------------

class DashboardTests(Base):
    def test_status_line(self):
        self.assertEqual(launcher.fitness(self.me), "Rest day")
        program = programs.copy_template(self.me, "full_body")
        ProgramDay.objects.filter(program=program, name="Full body A").update(weekdays=[self.today.weekday()])
        ProgramDay.objects.filter(program=program, name="Full body B").update(weekdays=[(self.today.weekday() + 2) % 7])
        self.assertEqual(launcher.fitness(self.me), "Today: Full body A")
        self.assertEqual(launcher.LINES["fitness"], launcher.fitness)
        day = program.days.get(name="Full body A")
        w = programs.start_workout(self.me, day=day)
        programs.finish(w)
        self.assertEqual(launcher.fitness(self.me), "Done today: Full body A")
        self.assertEqual(tiles.fitness(self.me)["value"], "Full body A")

    def test_week_streak_and_volume(self):
        program = programs.copy_template(self.me, "full_body")
        Program.objects.filter(pk=program.pk).update(created_at=timezone.now() - timedelta(days=60))
        monday = stats.week_start(self.today)
        for weeks_back in (1, 2):                                            # two full weeks done (3 of 3 each)
            for offset in (0, 2, 4):
                self.workout(day=monday - timedelta(weeks=weeks_back) + timedelta(days=offset), sets=[(225, 5)])
        self.workout(day=monday - timedelta(weeks=3), sets=[(225, 5)])       # week 3: 1 of 3 - the streak stops
        self.assertEqual(stats.streak(self.me), 2)
        self.workout(day=self.today, exercise="Barbell Bench Press", sets=[(185, 8), (185, 8)])
        volume = {r["muscle"]: r for r in stats.volume_by_muscle(self.me)}
        self.assertEqual(volume["chest"]["sets"], 2)
        self.assertEqual(volume["chest"]["pounds"], D("2960.00"))
        page = self.client.get(url("dashboard")).content.decode()
        self.assertIn('id="fx-streak"', page)
        self.assertIn("Chest", page.split('id="fx-volume-h"', 1)[1])

    def test_todays_workout_has_a_start_button(self):
        program = programs.copy_template(self.me, "full_body")
        ProgramDay.objects.filter(program=program, name="Full body A").update(weekdays=[self.today.weekday()])
        today = self.client.get(url("dashboard")).content.decode().split('id="fx-today"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Full body A", today)
        self.assertIn(">Start</button>", today)

    def test_exercise_chart(self):
        for weeks_back, weight in ((3, 205), (2, 215), (1, 225)):
            self.workout(day=self.today - timedelta(weeks=weeks_back), sets=[(weight, 5)])
        page = self.client.get(url("exercise", self.ex("Barbell Back Squat").pk)).content.decode()
        self.assertEqual(len(re.findall(r'<span class="fx-chart__dot"', page)), 3)
        self.assertIn('data-label="', page)                                  # the hover readout
        self.assertIn("Show as a table", page)                               # and the numbers as a table
        self.assertIn("262.5 lb", page)                                      # 225 x 5 -> Epley 262.5
