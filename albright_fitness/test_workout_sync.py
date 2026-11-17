"""One done state for scheduled workouts (workout_sync): checking a program workout task in the Planner / Home makes or
finishes the workout in Fitness, finishing in Fitness checks the task, unchecking removes only an empty auto-made
workout, the backfill; the stale "Continue" banner; the shared segmented control on the Quick log tabs."""
import re
from datetime import timedelta
from io import StringIO

from django.apps import apps
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from albright_planner import planner
from albright_planner.models import ItemCompletion, ScheduleItem

from . import body, programs, weigh_in, workout_sync
from .models import Program, ProgramDay, ProgramExercise, WeighInLog, WeighInSchedule, WeightEntry, Workout, WorkoutSet
from .tests import Base, static, url

AJAX = {"HTTP_X_REQUESTED_WITH": "XMLHttpRequest"}


class SyncBase(Base):
    def setUp(self):
        super().setUp()
        self.program = Program.objects.create(owner=self.me, name="Strength")
        self.day = ProgramDay.objects.create(program=self.program, name="Legs", weekdays=[self.today.weekday()])
        ProgramExercise.objects.create(day=self.day, exercise=self.ex("Barbell Back Squat"), target_sets=3, target_reps=5)
        programs.schedule(self.program, start=self.today)
        self.item = ScheduleItem.objects.get(owner=self.me, source_id=f"day-{self.day.pk}")

    def check(self):
        """The Planner's (and Home's Today panel's) check-off - the same view."""
        data = {"date": self.today.isoformat()}
        return self.client.post(reverse("albright_planner:toggle", args=[self.item.pk]), data, **AJAX).json()

    def is_done(self):
        return ItemCompletion.objects.filter(item=self.item, occurrence_date=self.today).exists()

    def today_card(self):
        page = self.client.get(url("dashboard")).content.decode()
        return page, page.split('id="fx-today"', 1)[1].split("</section>", 1)[0]


class PlannerToFitnessTests(SyncBase):
    def test_checking_the_task_makes_a_completed_workout_and_today_shows_done(self):
        _page, card = self.today_card()
        self.assertIn("fx-start", card)
        self.assertTrue(self.check()["done"])
        w = Workout.objects.get(owner=self.me)
        self.assertTrue(w.finished)
        self.assertTrue(w.from_planner)
        self.assertEqual((w.date, w.program_day, w.planner_item, w.planner_date), (self.today, self.day, self.item, self.today))
        self.assertFalse(WorkoutSet.objects.filter(workout_exercise__workout=w).exists())     # no sets required
        page, card = self.today_card()
        self.assertNotIn("fx-start", card)                                   # no Start button
        self.assertIn("Done &middot; from Planner", card)
        self.assertNotIn("Start Legs", page)                                  # Quick log's start button for that day
        summary = self.client.get(url("summary", w.pk)).content.decode()
        self.assertIn("Completed from Planner", summary)

    def test_checking_again_never_duplicates(self):
        self.check()
        planner.toggle(self.item, self.today, self.me, done=True)             # e.g. the Bot API, already done
        workout_sync.complete_scheduled_workout(self.item, self.today, self.me)
        self.assertEqual(Workout.objects.filter(owner=self.me).count(), 1)

    def test_a_workout_already_started_today_is_finished_not_duplicated(self):
        started = programs.start_workout(self.me, day=self.day)              # from Fitness, not the task
        self.check()
        self.assertEqual(Workout.objects.filter(owner=self.me).count(), 1)
        started.refresh_from_db()
        self.assertTrue(started.finished)
        self.assertFalse(started.from_planner)
        self.assertEqual((started.planner_item, started.planner_date), (self.item, self.today))

    def test_a_workout_already_logged_today_counts_as_is(self):
        logged = self.workout(sets=[(225, 5)])
        logged.program_day = self.day
        logged.save()
        self.check()
        self.assertEqual(list(Workout.objects.filter(owner=self.me)), [logged])

    def test_unchecking_deletes_only_an_empty_auto_made_workout(self):
        self.check()
        data = self.check()
        self.assertFalse(data["done"])
        self.assertNotIn("note", data)
        self.assertFalse(Workout.objects.filter(owner=self.me).exists())
        _page, card = self.today_card()
        self.assertIn("fx-start", card)                                       # back to Start

    def test_unchecking_keeps_a_workout_with_sets_and_says_so(self):
        self.check()
        w = Workout.objects.get(owner=self.me)
        we = w.exercises.create(exercise=self.ex("Barbell Back Squat"))
        WorkoutSet.objects.create(workout_exercise=we, weight=225, reps=5, done=True)
        data = self.check()
        self.assertFalse(data["done"])
        self.assertEqual(data["note"], "Workout kept - edit it in Fitness.")
        self.assertTrue(Workout.objects.filter(pk=w.pk).exists())
        self.assertFalse(self.is_done())

    def test_bot_api_toggle_goes_through_the_same_sync(self):
        from albright_bot_api.schedule_api import _set_done  # noqa: F401 - the endpoint calls planner.toggle
        planner.toggle(self.item, self.today, self.me, done=True)
        self.assertTrue(Workout.objects.get(owner=self.me).from_planner)
        planner.toggle(self.item, self.today, self.me, done=False)
        self.assertFalse(Workout.objects.filter(owner=self.me).exists())


class FitnessToPlannerTests(SyncBase):
    def test_finishing_a_workout_in_fitness_checks_the_task(self):
        self.client.post(url("start"), {"day": self.day.pk})
        w = Workout.objects.get(owner=self.me, finished_at__isnull=True)
        self.assertFalse(self.is_done())
        self.client.post(url("finish", w.pk))
        self.assertTrue(self.is_done())
        self.assertEqual(Workout.objects.filter(owner=self.me).count(), 1)    # no extra "from Planner" one

    def test_a_program_day_workout_not_started_from_the_task_still_checks_it(self):
        w = programs.start_workout(self.me, day=self.day)
        Workout.objects.filter(pk=w.pk).update(planner_item=None, planner_date=None)
        w.refresh_from_db()
        programs.finish(w)
        self.assertTrue(self.is_done())
        self.assertEqual(Workout.objects.filter(owner=self.me).count(), 1)


class BackfillTests(SyncBase):
    def test_checked_tasks_with_no_workout_get_one_and_rerunning_changes_nothing(self):
        ItemCompletion.objects.create(item=self.item, occurrence_date=self.today, completed_by=self.me)  # pre-sync
        out = StringIO()
        call_command("sync_workout_task_completion", stdout=out)
        self.assertIn("Made 1", out.getvalue())
        w = Workout.objects.get(owner=self.me)
        self.assertTrue(w.from_planner and w.finished)
        self.assertEqual(w.program_day, self.day)
        call_command("sync_workout_task_completion", stdout=StringIO())
        self.assertEqual(Workout.objects.filter(owner=self.me).count(), 1)
        _page, card = self.today_card()
        self.assertNotIn("fx-start", card)

    def test_backfill_leaves_occurrences_that_have_a_workout(self):
        logged = self.workout(sets=[(225, 5)])
        logged.program_day = self.day
        logged.save()
        ItemCompletion.objects.create(item=self.item, occurrence_date=self.today, completed_by=self.me)
        self.assertEqual(workout_sync.backfill(apps.get_model), 0)


class WeighInSyncTests(Base):
    def setUp(self):
        super().setUp()
        form = weigh_in.ScheduleForm({"frequency": "daily", "start_date": self.today.isoformat(), "time_of_day": "07:00"})
        self.assertTrue(form.is_valid(), form.errors)
        self.schedule = weigh_in.save(self.me, form)
        self.item = WeighInSchedule.objects.get(pk=self.schedule.pk).planner_item

    def test_checking_off_without_a_weight_uses_that_days_weight_or_is_a_skip(self):
        planner.toggle(self.item, self.today, self.me, done=True)
        self.assertTrue(WeighInLog.objects.get(item=self.item, occurrence_date=self.today).skipped)
        planner.toggle(self.item, self.today, self.me, done=False)
        entry = WeightEntry.objects.create(owner=self.me, date=self.today, weight_kg=80)
        planner.toggle(self.item, self.today, self.me, done=True)
        log = WeighInLog.objects.get(item=self.item, occurrence_date=self.today)
        self.assertEqual((log.weight_entry, log.skipped), (entry, False))

    def test_removing_the_weight_reopens_the_weigh_in(self):
        entry, _ = body.log_weight(self.me, self.today, 180, "lb")
        self.assertTrue(ItemCompletion.objects.filter(item=self.item, occurrence_date=self.today).exists())
        self.client.post(url("delete_weight", entry.pk))
        self.assertFalse(ItemCompletion.objects.filter(item=self.item, occurrence_date=self.today).exists())


class StaleWorkoutTests(Base):
    def test_continue_banner_only_for_the_last_24_hours(self):
        w = self.workout(finished=False)
        page = self.client.get(url("dashboard")).content.decode()
        self.assertIn('id="fx-resume"', page)
        self.assertNotIn('id="fx-stale"', page)
        old = self.today - timedelta(days=5)
        Workout.objects.filter(pk=w.pk).update(date=old, created_at=timezone.now() - timedelta(days=5))
        page = self.client.get(url("dashboard")).content.decode()
        self.assertNotIn('id="fx-resume"', page)
        stale = page.split('id="fx-stale"', 1)[1].split("</div>", 1)[0]
        self.assertIn("Unfinished workout from", stale)
        self.assertIn(f"{old:%b} {old.day}", stale)
        self.assertIn('id="fx-stale-finish"', stale)
        self.assertIn('id="fx-stale-discard"', stale)
        response = self.client.post(url("delete_workout", w.pk), {"back": "today"})
        self.assertRedirects(response, url("dashboard"), fetch_redirect_response=False)
        self.assertFalse(Workout.objects.filter(pk=w.pk).exists())


class SegmentedControlTests(Base):
    def test_quick_log_tabs_use_the_shared_segmented_control(self):
        page = self.client.get(url("dashboard")).content.decode()
        tabs = page.split("data-quick-tabs", 1)[1].split("</div>", 1)[0]
        self.assertIn('role="tablist"', page.split("data-quick-tabs", 1)[0][-200:])
        buttons = re.findall(r"<button[^>]*role=\"tab\"[^>]*>", tabs)
        self.assertGreaterEqual(len(buttons), 2)
        self.assertTrue(all("fx-seg__item" in b for b in buttons))
        self.assertEqual(sum('aria-selected="true"' in b for b in buttons), 1)
        self.assertIn('aria-selected="true"', buttons[0])
        self.assertNotIn("fx-tabs", page)
        css = static("albright_fitness/fitness.css")
        rule = css.split(".fx-seg__item {", 1)[1].split("}", 1)[0]
        for reset in ("appearance: none", "background: transparent", "border: 1px solid transparent"):
            self.assertIn(reset, rule)
        self.assertIn('.fx-seg__item[aria-selected="true"]', css)
        self.assertNotRegex(css.split(".fx-seg {", 1)[1].split("@media (forced", 1)[0], r"#fff|:\s*white\b")
