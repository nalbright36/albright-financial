"""Slice 1 of the Fitness usability pass: plain-words help for terms (glossary.py, {% term %}), units set in one
place (Body > Settings), My foods and Targets reachable from Today and the top of the diary, the landing-page list
naming Fitness's real pages, and small wording fixes (estimated 1-rep max to one decimal, "1 of 4 workouts")."""
from decimal import Decimal

from albright_labs import apps_registry

from . import glossary, programs, training, usda
from .models import NutritionProfile, PersonalRecord, Workout
from .tests import Base, url


class GlossaryTests(Base):
    def test_every_term_has_a_label_and_plain_text(self):
        for key, (label, text) in glossary.TERMS.items():
            self.assertTrue(label and len(text) > 20, key)

    def test_term_shows_on_insights_and_today(self):
        page = self.client.get(url("insights")).content.decode()
        self.assertIn('aria-label="What is Volume?"', page)
        self.assertIn('aria-label="What is Estimated 1-rep max?"', page)
        self.assertIn('aria-label="What is Working sets?"', self.client.get(url("dashboard")).content.decode())

    def test_open_workout_explains_sets_finished_does_not(self):
        w = Workout.objects.create(owner=self.me, date=self.today)
        page = self.client.get(url("workout", w.pk)).content.decode()
        self.assertIn('id="fx-guide"', page)
        self.assertIn("How hard the set felt", page)
        programs.finish(w)
        self.assertNotIn('id="fx-guide"', self.client.get(url("workout", w.pk)).content.decode())

    def test_program_add_row_spells_out_minutes_and_miles(self):
        p = programs.copy_template(self.me, "full_body")
        page = self.client.get(url("program", p.pk)).content.decode()
        self.assertIn("<span>Minutes</span>", page)
        self.assertIn("<span>Miles</span>", page)
        self.assertIn("its days show on your Today page", page)


class UnitsInOnePlaceTests(Base):
    def test_targets_links_to_body_settings_instead_of_its_own_menus(self):
        page = self.client.get(url("targets")).content.decode()
        self.assertNotIn('name="weight_unit"', page)
        self.assertIn("?settings=1#fx-body-settings", page)

    def test_body_settings_opens_from_the_link_and_still_saves_units(self):
        self.assertIn('id="fx-body-settings" open', self.client.get(url("body") + "?settings=1").content.decode())
        self.assertNotIn('id="fx-body-settings" open', self.client.get(url("body")).content.decode())
        self.client.post(url("body_settings"), {"weight_unit": "kg", "height_unit": "cm"})
        profile = NutritionProfile.load(self.me)
        self.assertEqual((profile.weight_unit, profile.height_unit), ("kg", "cm"))
        self.assertIn("Units: kg &middot; cm.", self.client.get(url("targets")).content.decode())

    def test_old_units_post_on_targets_still_works(self):
        self.client.post(url("targets"), {"action": "units", "weight_unit": "kg", "height_unit": "in"})
        self.assertEqual(NutritionProfile.load(self.me).weight_unit, "kg")


class ReachTests(Base):
    def test_my_foods_and_targets_from_today_and_the_top_of_the_diary(self):
        today = self.client.get(url("dashboard")).content.decode()
        self.assertIn('id="fx-food-links"', today)
        diary = self.client.get(url("food")).content.decode()
        self.assertIn('id="fx-food-top-links"', diary)
        for page in (today, diary):
            self.assertIn(url("my_foods"), page)
            self.assertIn(url("targets"), page)

    def test_landing_list_names_the_real_pages(self):
        labels = [label for _key, label, _u in apps_registry.BY_KEY["fitness"].pages]
        self.assertEqual(labels, ["Today", "Food", "History", "Insights", "Body", "Programs", "Exercises", "Challenges"])
        self.assertEqual(apps_registry.BY_KEY["fitness"].pages[0][0], "fitness:dashboard")   # saved choices keep working
        for key, _label, name in apps_registry.BY_KEY["fitness"].pages:
            self.assertEqual(self.client.get(url(name.split(":")[1])).status_code, 200, key)


class WordingTests(Base):
    def test_estimated_max_one_decimal_weight_stays_exact(self):
        ex = self.ex("Barbell Bench Press")
        e1rm = PersonalRecord(owner=self.me, exercise=ex, kind=PersonalRecord.E1RM, value=Decimal("234.33"),
                              previous=Decimal("228.67"), achieved_on=self.today)
        self.assertEqual(training.record_text(e1rm), "Best estimated 1-rep max: 234.3 lb (was 228.7 lb)")
        heavy = PersonalRecord(owner=self.me, exercise=ex, kind=PersonalRecord.WEIGHT, value=Decimal("187.5"),
                               achieved_on=self.today)
        self.assertEqual(training.record_text(heavy), "Heaviest weight: 187.5 lb")

    def test_week_line_plural_follows_planned(self):
        p = programs.copy_template(self.me, "full_body")
        p.days.update(weekdays=[0, 1, 2, 3, 4, 5, 6])
        w = Workout.objects.create(owner=self.me, date=self.today, program_day=p.days.first())
        programs.finish(w)
        page = self.client.get(url("dashboard")).content.decode()
        self.assertRegex(page, r"1 of \d+ workouts")
        self.assertNotRegex(page, r"1 of \d+ workout ")

    def test_usda_message_is_plain(self):
        self.assertIn("Food search isn't set up", usda.NOT_SET)
        self.assertIn("USDA key not set", usda.NOT_SET)

    def test_challenges_without_a_circle_links_to_circles(self):
        from django.urls import reverse
        page = self.client.get(url("challenges")).content.decode()
        if "Challenges are played with a circle" in page:
            self.assertIn(reverse("labs:circles"), page)
