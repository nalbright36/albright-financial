"""Logging from an exercise page (the Log card and its bottom sheet), the Today page's Quick log (workout preview with
unknown-name matching, food, weight), Schedule (an exercise, a program, a program day - one-off and repeating - as
my own Planner tasks through the Planner's service helpers, never blocked by the automatic-source rules), Fitness
tasks in the Planner (yellow and checkable; meals stay orange and view-only; the "Log this workout?" prompt; the view
panel; finishing or logging completes the occurrence) and Home's "Meals today" / "Workouts today"."""
import re
from datetime import time, timedelta
from decimal import Decimal

from django.urls import reverse

from albright_labs.models import UserPreference
from albright_labs.testing import grants_of, kitchen, with_apps
from albright_planner import sources
from albright_planner.models import DeletedSourceItem, ItemCompletion, PlannerSettings, ScheduleItem

from . import programs, schedule, training
from .models import (CustomFood, FoodLogEntry, NutritionProfile, PersonalRecord, ScheduledSession, WeightEntry, Workout,
                     WorkoutExercise)
from .tests import Base, static, url

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


def log_before(user, text, day):
    """A finished workout on `day` from the quick text."""
    workout, _records, _day = programs.log_parsed(user, training.parse_quick(text, user), today=day)
    return workout


class LogCardTests(Base):
    def test_card_comes_first_prefilled_from_the_suggestion(self):
        bench = self.ex("Barbell Bench Press")
        log_before(self.me, "bench 185x8x3", self.today - timedelta(days=3))
        page = self.client.get(url("exercise", bench.pk)).content.decode()
        self.assertLess(page.index('id="fx-log"'), page.index('id="fx-chart-h"'))       # above Progress
        card = page.split('id="fx-log"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Last time", card)
        self.assertIn("185×8, 185×8, 185×8", card)
        self.assertIn("187.5", card)                                                       # +2.5 lb upper body
        self.assertEqual(card.count('name="weight" '), 5)          # 3 suggested + the no-JS spare + the template
        self.assertEqual(len(re.findall(r'name="reps"[^>]*value="8"', card)), 3)
        self.assertIn("Log as new workout", card)
        self.assertIn("Start a full workout with this", card)
        self.assertIn("+ Set", card)

    def test_save_logs_a_new_finished_workout_today_with_records(self):
        bench = self.ex("Barbell Bench Press")
        log_before(self.me, "bench 185x8x3", self.today - timedelta(days=3))
        response = self.client.post(url("exercise_log", bench.pk),
                                    {"weight": ["190", "190", ""], "reps": ["8", "7", ""], "unit": "lb"}, **AJAX)
        data = response.json()
        self.assertTrue(data["ok"], data)
        self.assertFalse(data["added_to_open"])
        w = Workout.objects.get(owner=self.me, date=self.today)
        self.assertTrue(w.finished)
        sets = list(w.exercises.get().sets.all())
        self.assertEqual([(s.weight, s.reps, s.done) for s in sets], [(Decimal("190"), 8, True), (Decimal("190"), 7, True)])
        self.assertTrue(any(t.startswith("Heaviest weight: 190 lb") for t in data["records"]), data["records"])
        self.assertIn("fx-chart", data["progress_html"])                                  # the page's cards update
        self.assertIn("Heaviest weight", data["records_html"])

    def test_save_adds_to_todays_workout_in_progress_and_records_are_not_doubled(self):
        bench = self.ex("Barbell Bench Press")
        log_before(self.me, "bench 185x8x3", self.today - timedelta(days=3))
        open_workout = programs.start_workout(self.me, name="Push day")
        page = self.client.get(url("exercise", bench.pk)).content.decode()
        self.assertIn("Add to today's workout", page)
        data = self.client.post(url("exercise_log", bench.pk), {"weight": ["195"], "reps": ["5"]}, **AJAX).json()
        self.assertTrue(data["added_to_open"])
        self.assertIn("Push day", data["message"])
        open_workout.refresh_from_db()
        self.assertFalse(open_workout.finished)
        self.assertEqual(open_workout.exercises.get().exercise, bench)
        self.assertEqual(PersonalRecord.objects.filter(workout=open_workout, kind="weight").count(), 1)
        self.client.post(url("finish", open_workout.pk))
        self.assertEqual(PersonalRecord.objects.filter(workout=open_workout, kind="weight").count(), 1)

    def test_cardio_rows_and_bad_rows(self):
        run = self.ex("Running")
        data = self.client.post(url("exercise_log", run.pk), {"duration": ["28:30"], "distance": ["3.1"],
                                                              "distance_unit": "mi"}, **AJAX).json()
        self.assertTrue(data["ok"])
        s = WorkoutExercise.objects.get(exercise=run).sets.get()
        self.assertEqual((s.duration_seconds, s.distance), (1710, Decimal("3.10")))
        bad = self.client.post(url("exercise_log", self.ex("Barbell Bench Press").pk), {"weight": ["100"], "reps": [""]}, **AJAX)
        self.assertEqual(bad.status_code, 400)
        self.assertFalse(bad.json()["ok"])

    def test_sheet_and_the_exercises_list_log_button(self):
        bench = self.ex("Barbell Bench Press")
        sheet = self.client.get(url("exercise_log", bench.pk)).content.decode()
        self.assertIn("Log Barbell Bench Press", sheet)
        self.assertIn('data-log-card', sheet)
        listing = self.client.get(url("exercises") + "?q=bench").content.decode()
        self.assertIn(f'data-sheet-url="{url("exercise_log", bench.pk)}?next=', listing)
        self.assertIn('id="fx-sheet"', listing)
        js = static("albright_fitness/fitness.js")
        self.assertIn("[data-sheet-url]", js)
        self.assertIn("addSet(", js)

    def test_start_a_full_workout_with_this(self):
        squat = self.ex("Barbell Back Squat")
        response = self.client.post(url("start"), {"exercise": squat.pk})
        w = Workout.objects.get(owner=self.me)
        self.assertRedirects(response, url("workout", w.pk))
        first = w.exercises.first()
        self.assertEqual(first.exercise, squat)
        self.assertEqual(first.sets.count(), 3)

    def test_someone_elses_exercise_is_a_404(self):
        mine = self.ex("Barbell Bench Press").__class__.objects.create(owner=self.friend, name="Kim's lift")
        self.assertEqual(self.client.get(url("exercise_log", mine.pk)).status_code, 404)
        self.assertEqual(self.client.post(url("exercise_log", mine.pk), {"reps": ["5"]}).status_code, 404)


class QuickLogTests(Base):
    def test_preview_flags_unknown_names_with_a_picker(self):
        data = self.client.post(url("quick_preview"), {"text": "bench 185x8,8,7, zorbs 10x3"}, **AJAX).json()
        self.assertFalse(data["ready"])
        self.assertIn("Barbell Bench Press", data["html"])
        self.assertIn("185×8, 185×8, 185×7", data["html"])
        self.assertIn("&ldquo;zorbs&rdquo;?", data["html"])
        self.assertIn('name="match-1"', data["html"])

    def test_a_picked_match_is_previewed_and_saved(self):
        pushup = self.ex("Push-Up")
        data = self.client.post(url("quick_preview"), {"text": "bench 185x8,8,7, zorbs 10,10,10", "match-1": pushup.pk}, **AJAX).json()
        self.assertTrue(data["ready"])
        self.assertIn(f'<input type="hidden" name="match-1" value="{pushup.pk}">', data["html"])
        self.client.post(url("quick"), {"text": "bench 185x8,8,7, zorbs 10,10,10", "match-1": pushup.pk})
        w = Workout.objects.get(owner=self.me)
        self.assertEqual([we.exercise.name for we in w.exercises.all()], ["Barbell Bench Press", "Push-Up"])
        self.assertEqual([s.reps for s in w.exercises.get(exercise=pushup).sets.all()], [10, 10, 10])

    def test_card_tabs_recent_chips_and_start_buttons(self):
        for n, name in enumerate(["bench 100x5", "squat 100x5", "deadlift 100x5", "ohp 50x5", "row 80x5", "curl 30x5",
                                  "pullups 8", "pushups 20", "dips 10"]):
            log_before(self.me, name, self.today - timedelta(days=20 - n))
        program = programs.copy_template(self.me, "full_body")
        day = program.days.get(name="Full body A")
        day.weekdays = [self.today.weekday()]
        day.save()
        page = self.client.get(url("dashboard")).content.decode()
        card = page.split('id="fx-quicklog"', 1)[1].split("</section>", 1)[0]
        self.assertLess(page.index('id="fx-quicklog"'), page.index('id="fx-today"'))   # near the top
        for tab in ("Workout", "Food", "Weight"):
            self.assertIn(f'data-quick-tab="{tab.lower()}">{tab}</button>', card)
        chips = re.findall(r'class="fx-chip" data-sheet-url="[^"]+">([^<]+)</a>', card)
        self.assertEqual(len(chips), 8)
        self.assertEqual(chips[0], "Dip")                                                # newest first
        self.assertIn("Start empty workout", card)
        self.assertIn("Start Full body A", card)
        self.assertIn("fx-quick-tab", static("albright_fitness/fitness.js"))          # the last tab is remembered

    def test_food_quick_log_my_food_recent_and_quick_add(self):
        food = CustomFood.objects.create(owner=self.me, name="Protein shake", serving_label="1 scoop", calories=120, protein=24)
        page = self.client.get(url("dashboard")).content.decode()
        self.assertIn(f'name="pick" value="custom:{food.pk}"', page)
        self.client.post(url("food_log"), {"pick": f"custom:{food.pk}", "servings": "2", "slot": "lunch",
                                           "date": self.today.isoformat(), "next": url("dashboard")})
        entry = FoodLogEntry.objects.get(owner=self.me)
        self.assertEqual((entry.label, entry.slot, entry.servings, entry.calories), ("Protein shake", "lunch", Decimal("2"), Decimal("240")))
        page = self.client.get(url("dashboard")).content.decode()
        self.assertIn(f'name="pick" value="recent:{entry.pk}"', page)
        self.client.post(url("food_log"), {"pick": f"recent:{entry.pk}", "slot": "snack", "date": self.today.isoformat()})
        self.client.post(url("food_log"), {"kind": "quick", "calories": "300", "protein": "10", "slot": "dinner",
                                           "date": self.today.isoformat()})
        self.assertEqual(FoodLogEntry.objects.filter(owner=self.me).count(), 3)
        self.assertTrue(FoodLogEntry.objects.filter(owner=self.me, source="quick", calories=300).exists())

    def test_weight_quick_log_and_hidden_when_weight_is_off(self):
        page = self.client.get(url("dashboard")).content.decode()
        self.assertIn('id="fx-quick-weight"', page)
        self.client.post(url("log_weight"), {"weight": "180", "unit": "lb", "date": self.today.isoformat(),
                                             "next": url("dashboard")})
        self.assertTrue(WeightEntry.objects.filter(owner=self.me, date=self.today).exists())
        profile = NutritionProfile.load(self.me)
        profile.hide_weight = True
        profile.save()
        page = self.client.get(url("dashboard")).content.decode()
        self.assertNotIn('id="fx-quick-weight"', page)
        self.assertNotIn('data-quick-tab="weight"', page)


class ScheduleTests(Base):
    def schedule(self, kind, pk, **fields):
        data = {"date": self.today.isoformat(), "repeat": "none", "interval": "2", **fields}
        return self.client.post(url("schedule_new", kind, pk), data)

    def test_one_off_exercise_with_a_time_and_target(self):
        squat = self.ex("Barbell Back Squat")
        self.schedule("exercise", squat.pk, time="07:30", sets="3", reps="5")
        session = ScheduledSession.objects.get(owner=self.me)
        item = session.planner_item
        self.assertEqual((item.owner, item.source_app, item.source_id), (self.me, "fitness", f"session-{session.pk}"))
        self.assertEqual((item.kind, item.checkable, item.recurrence, item.start_time), ("task", True, "", time(7, 30)))
        self.assertEqual(item.title, "Barbell Back Squat")
        self.assertEqual(item.source_url, f"/fitness/start/?session={session.pk}")
        self.assertFalse(bool(grants_of(item)))                                         # private
        page = self.client.get(url("exercise", squat.pk)).content.decode()
        listed = page.split('id="fx-scheduled"', 1)[1].split("</section>", 1)[0]
        self.assertIn("3 × 5", listed)
        self.assertIn(url("schedule_edit", session.pk), listed)
        self.assertIn(url("schedule_remove", session.pk), listed)

    def test_repeating_on_mon_wed_fri(self):
        self.schedule("exercise", self.ex("Running").pk, repeat="weekly", days=["MO", "WE", "FR"])
        item = ScheduleItem.objects.get(source_app="fitness")
        self.assertEqual(item.recurrence, "FREQ=WEEKLY;BYDAY=MO,WE,FR")

    def test_program_day_and_program_checklists(self):
        program = programs.copy_template(self.me, "full_body")
        day = program.days.get(name="Full body A")
        self.schedule("day", day.pk)
        item = ScheduledSession.objects.get(program_day=day).planner_item
        self.assertEqual([r.text for r in item.checklist.all()],
                         [f"{pe.exercise.name} - {pe.target_text}" for pe in day.exercises.all()])
        self.assertEqual(item.checklist.first().text, "Barbell Back Squat - 3 × 5")
        self.schedule("day", day.pk, sets="5", reps="5", repeat="weekly", days=["TU"])
        item = ScheduledSession.objects.filter(program_day=day).last().planner_item
        self.assertEqual(item.recurrence, "FREQ=WEEKLY;BYDAY=TU")
        self.assertTrue(all(text.endswith(" - 5 × 5") for text in item.checklist.values_list("text", flat=True)))
        self.schedule("program", program.pk)
        item = ScheduledSession.objects.get(program=program).planner_item
        texts = list(item.checklist.values_list("text", flat=True))
        self.assertTrue(texts[0].startswith("Full body A: "))
        self.assertTrue(any(t.startswith("Full body B: ") for t in texts))
        page = self.client.get(url("program", program.pk)).content.decode()
        self.assertEqual(page.split('id="fx-scheduled"', 1)[1].split("</section>", 1)[0].count('class="fx-row fx-sched"'), 3)

    def test_not_blocked_by_the_automatic_source_rules(self):
        self.assertEqual(PlannerSettings.for_user(self.me).sources_on, [])            # every automatic source off
        bench = self.ex("Barbell Bench Press")
        self.schedule("exercise", bench.pk)
        first = ScheduleItem.objects.get(source_app="fitness")
        sources.sync(self.me)                                                           # a sync leaves it alone
        first.refresh_from_db()
        self.assertFalse(first.archived)
        # Deleted in the Planner: only that schedule goes - and scheduling again still works.
        self.client.post(reverse("albright_planner:remove", args=[first.pk]), {"date": first.date.isoformat(), "scope": "all"})
        self.assertFalse(ScheduleItem.objects.filter(pk=first.pk).exists())
        self.assertFalse(ScheduledSession.objects.exists())
        self.assertTrue(DeletedSourceItem.objects.filter(source_app="fitness").exists())
        self.schedule("exercise", bench.pk)
        self.assertEqual(ScheduleItem.objects.filter(source_app="fitness", archived=False).count(), 1)

    def test_deleting_one_removes_only_that_schedule(self):
        bench = self.ex("Barbell Bench Press")
        self.schedule("exercise", bench.pk)
        self.schedule("exercise", bench.pk, date=(self.today + timedelta(days=2)).isoformat())
        one, two = ScheduledSession.objects.order_by("pk")
        self.client.post(url("schedule_remove", one.pk))
        self.assertEqual(list(ScheduledSession.objects.all()), [two])
        self.assertEqual(ScheduleItem.objects.filter(source_app="fitness").count(), 1)

    def test_edit_changes_the_task_in_place(self):
        bench = self.ex("Barbell Bench Press")
        self.schedule("exercise", bench.pk)
        session = ScheduledSession.objects.get()
        later = self.today + timedelta(days=1)
        self.client.post(url("schedule_edit", session.pk), {"date": later.isoformat(), "repeat": "daily", "sets": "4",
                                                            "reps": "6", "interval": "2"})
        item = ScheduleItem.objects.get(pk=session.planner_item_id)
        self.assertEqual((item.date, item.recurrence), (later, "FREQ=DAILY"))
        session.refresh_from_db()
        self.assertEqual(session.target_text, "4 × 6")

    def test_other_peoples_schedules_are_404s(self):
        self.schedule("exercise", self.ex("Barbell Bench Press").pk)
        session = ScheduledSession.objects.get()
        self.client.force_login(self.friend)
        self.assertEqual(self.client.get(url("schedule_edit", session.pk)).status_code, 404)
        self.assertEqual(self.client.post(url("schedule_remove", session.pk)).status_code, 404)
        program = programs.copy_template(self.me, "full_body")
        self.assertEqual(self.schedule("program", program.pk).status_code, 404)


class PlannerTests(Base):
    def scheduled(self, exercise_name="Barbell Bench Press", **fields):
        data = {"date": self.today.isoformat(), "repeat": "none", "interval": "2", **fields}
        self.client.post(url("schedule_new", "exercise", self.ex(exercise_name).pk), data)
        return ScheduledSession.objects.filter(owner=self.me).last()

    def test_yellow_and_checkable_while_meals_stay_orange_and_view_only(self):
        kitchen(self.me)
        from albright_labs.models import Space
        from albright_meals.models import MealPlanEntry
        MealPlanEntry.objects.create(space=Space.objects.filter(circle__memberships__user=self.me).first(),
                                     date=self.today, slot="dinner", title="Tacos")
        PlannerSettings.objects.update_or_create(user=self.me, defaults={"show_meals": True})
        item = self.scheduled().planner_item
        for view in ("day", "week"):
            page = self.client.get(reverse(f"albright_planner:{view}") + f"?date={self.today}").content.decode()
            row = page.split(f'data-item="{item.pk}"', 1)[0].rsplit("<li", 1)[1] + page.split(f'data-item="{item.pk}"', 1)[1].split("</li>", 1)[0]
            self.assertIn("is-fitness", row, view)
            self.assertIn("pl-check-form", row)
            self.assertIn("pl-fit-icon", row)
            self.assertIn(f'data-log-url="/fitness/start/?session={item.fitness_session.pk}&amp;date={self.today}"', row)
            meal = page.split('class="pl-item is-meal"', 1)[1].split("</li>", 1)[0]
            self.assertNotIn("pl-check", meal)
        month = self.client.get(reverse("albright_planner:month") + f"?date={self.today}").content.decode()
        self.assertRegex(month, r'class="pl-chip[^"]*is-fitness"')
        self.assertIn('class="pl-chip is-meal"', month)
        css = static("albright_planner/planner.css")
        self.assertIn(".pl-item.is-fitness, .pl-chip.is-fitness, .pl-view.is-fitness { --pl-color: var(--app-fitness); }", css)
        self.assertIn(".pl-item.is-meal, .pl-chip.is-meal { --pl-color: var(--app-meals); }", css)

    def test_check_off_prompt_options(self):
        js = static("albright_planner/planner.js")
        for text in ("Log this workout?", ">Log now<", ">Mark done without logging<", 'value="cancel">Cancel<'):
            self.assertIn(text, js)
        self.assertIn('if (logChoice === "log") { window.location.href = logUrl; return null; }', js)

    def test_view_panel_shows_exercises_targets_last_time_and_start(self):
        log_before(self.me, "bench 185x8x3", self.today - timedelta(days=2))
        session = self.scheduled(sets="3", reps="8")
        panel = self.client.get(reverse("albright_planner:item", args=[session.planner_item_id]),
                                {"panel": "1", "date": self.today.isoformat()}).content.decode()
        section = panel.split('id="pl-view-fitness"', 1)[1].split("</section>", 1)[0]
        self.assertIn("Barbell Bench Press", section)
        self.assertIn("3 × 8", section)
        self.assertIn("Last time: 185×8, 185×8, 185×8", section)
        self.assertIn(f'href="/fitness/start/?session={session.pk}&amp;date={self.today}"', section)
        self.assertIn("Start workout", section)

    def test_log_now_prefills_and_finishing_completes_the_occurrence(self):
        session = self.scheduled(sets="4", reps="6")
        start = url("start") + f"?session={session.pk}&date={self.today}"
        page = self.client.get(start).content.decode()
        self.assertIn("Barbell Bench Press", page)
        self.assertIn("4 × 6", page)
        self.client.post(start, {"session": session.pk, "date": self.today.isoformat()})
        w = Workout.objects.get(owner=self.me, finished_at__isnull=True)
        self.assertEqual((w.planner_item_id, w.planner_date), (session.planner_item_id, self.today))
        we = w.exercises.get()
        self.assertEqual((we.target_sets, we.target_reps, we.sets.count()), (4, 6, 4))
        self.client.post(url("finish", w.pk))
        self.assertTrue(ItemCompletion.objects.filter(item_id=session.planner_item_id, occurrence_date=self.today).exists())

    def test_logging_the_same_exercise_that_day_completes_it(self):
        session = self.scheduled()
        self.client.post(url("quick"), {"text": "bench 185x5x3"})
        self.assertTrue(ItemCompletion.objects.filter(item_id=session.planner_item_id, occurrence_date=self.today).exists())
        # The Log card into a workout in progress: just that exercise counts.
        squat = self.scheduled("Barbell Back Squat")
        programs.start_workout(self.me, name="Legs")
        self.client.post(url("exercise_log", squat.exercise_id), {"weight": ["200"], "reps": ["5"]}, **AJAX)
        self.assertTrue(ItemCompletion.objects.filter(item_id=squat.planner_item_id, occurrence_date=self.today).exists())

    def test_a_program_day_completes_when_its_workout_finishes(self):
        program = programs.copy_template(self.me, "full_body")
        day = program.days.get(name="Full body B")
        self.client.post(url("schedule_new", "day", day.pk), {"date": self.today.isoformat(), "repeat": "none"})
        item = ScheduledSession.objects.get(program_day=day).planner_item
        w = programs.start_workout(self.me, day=day)
        self.assertFalse(ItemCompletion.objects.filter(item=item).exists())
        self.client.post(url("finish", w.pk))
        self.assertTrue(ItemCompletion.objects.filter(item=item, occurrence_date=self.today).exists())


class HomeTodayTests(Base):
    def setUp(self):
        super().setUp()
        pref = UserPreference.for_user(self.me)
        pref.show_today_panel = True
        pref.save()
        kitchen(self.me)
        from albright_labs.models import Space
        from albright_meals.models import MealPlanEntry
        MealPlanEntry.objects.create(space=Space.objects.filter(circle__memberships__user=self.me).first(),
                                     date=self.today, slot="dinner", title="Tacos")
        self.task = ScheduleItem.objects.create(owner=self.me, title="Call dentist", date=self.today)
        self.client.post(url("schedule_new", "exercise", self.ex("Running").pk),
                         {"date": self.today.isoformat(), "repeat": "none"})
        self.workout = ScheduledSession.objects.get().planner_item

    def panel(self):
        page = self.client.get(reverse("labs:hub")).content.decode()
        return page.split('id="planner-today"', 1)[1].split("</section>", 1)[0]

    def test_meals_and_workouts_today_above_the_open_tasks(self):
        panel = self.panel()
        meals, workouts, tasks = (panel.index('id="planner-today-meals"'), panel.index('id="planner-today-workouts"'),
                                  panel.index('id="planner-today-tasks"'))
        self.assertLess(meals, workouts)
        self.assertLess(workouts, tasks)
        meal_box = panel[meals:workouts]
        self.assertIn("Tacos", meal_box)
        self.assertNotIn("pl-check", meal_box)
        work_box = panel[workouts:tasks]
        self.assertIn("Running", work_box)
        self.assertIn("pl-check-form", work_box)
        self.assertIn("is-fitness", work_box)
        self.assertIn(">Start</a>", work_box)
        self.assertNotIn("Running", panel[tasks:])                                       # not listed twice
        self.assertIn("Call dentist", panel[tasks:])
        self.assertIn("2 open today", panel)

    def test_sections_respect_app_access(self):
        with_apps(self.me, ["planner", "notes"])
        panel = self.panel()
        self.assertNotIn('id="planner-today-meals"', panel)
        self.assertNotIn('id="planner-today-workouts"', panel)

    def test_show_completed_applies_to_workouts(self):
        ItemCompletion.objects.create(item=self.workout, occurrence_date=self.today)
        self.assertNotIn('id="planner-today-workouts"', self.panel())
        pref = UserPreference.for_user(self.me)
        pref.show_completed_today = True
        pref.save()
        panel = self.panel()
        box = panel.split('id="planner-today-workouts"', 1)[1].split("</ul>", 1)[0]
        self.assertIn("is-done", box)
        self.assertNotIn(">Start</a>", box)
