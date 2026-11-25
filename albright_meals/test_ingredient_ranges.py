"""Ingredient ranges like '4-5 stalks celery' keep their numbers on the recipe, when scaled,
and on the grocery list (the upper bound). Also a repair command for already-stored rows.
"""
from decimal import Decimal
from io import StringIO

from django.contrib.auth.models import User
from django.core.management import call_command
from django.test import TestCase

from albright_labs.testing import kitchen, with_apps

from . import groceries, importer, ingredients, meal_view
from .models import Recipe


class IngredientRangeTests(TestCase):
    def test_parse_ingredient_keeps_ranges_and_other_shapes(self):
        cases = [
            ("4-5 stalks celery", ("celery", "5", "stalk", "", "4")),
            ("4 - 5 stalks celery", ("celery", "5", "stalk", "", "4")),
            ("4–5 stalks celery", ("celery", "5", "stalk", "", "4")),  # en dash
            ("4 to 5 stalks celery", ("celery", "5", "stalk", "", "4")),
            ("1/2-1 cup sugar", ("sugar", "1", "cup", "", "0.5")),
            ("1 1/2-2 cups flour", ("flour", "2", "cup", "", "1.5")),
            ("1 (14 oz) can tomatoes", ("tomatoes", "1", "can", "14 oz", None)),
            ("2 large eggs", ("large eggs", "2", "", "", None)),
            ("a pinch salt", ("salt", "1", "pinch", "", None)),
            ("½ cup sugar", ("sugar", "0.5", "cup", "", None)),
            ("2-3 cloves garlic, minced", ("garlic", "3", "clove", "minced", "2")),
        ]
        for line, (name, quantity, unit, note, low) in cases:
            with self.subTest(line=line):
                parsed = importer.parse_ingredient(line)
                self.assertEqual((parsed["name"], parsed["quantity"], parsed["unit"], parsed["note"]),
                                 (name, quantity, unit, note))
                self.assertEqual(parsed.get("quantity_min") or None, low)
                self.assertEqual(parsed["original"], line)
                row = meal_view.ingredient_rows([parsed])[0]
                self.assertEqual(row["name"], name)
                if low:
                    self.assertIn("-", row["amount"])
                    self.assertEqual(row["qty_min"], float(groceries.parse_quantity(low)))
                    self.assertIsNotNone(row["qty"])

    def test_already_split_quantity_field_displays_the_range(self):
        """The live bug: imported or Bot-API rows stored quantity as '4-5' and unit as 'stalks'."""
        ident = "0123456789abcdef"
        stored = {"id": ident, "quantity": "4-5", "unit": "stalks", "name": "celery",
                  "original": "4-5 stalks celery"}
        row = meal_view.ingredient_rows([stored])[0]
        self.assertEqual(row["amount"], "4-5 stalks")
        self.assertEqual(row["name"], "celery")
        self.assertEqual(row["qty"], 5)
        self.assertEqual(row["qty_min"], 4)
        normalized = ingredients.normalize(stored)
        self.assertEqual(normalized["id"], ident)
        self.assertEqual(normalized["quantity"], "5")
        self.assertEqual(normalized["quantity_min"], "4")
        self.assertEqual(normalized["unit"], "stalk")
        self.assertEqual(normalized["name"], "celery")
        # Idempotent, and a second normalize keeps the same id.
        again = ingredients.normalize(normalized)
        self.assertEqual(again["id"], ident)
        self.assertEqual((again["quantity"], again["quantity_min"], again["unit"], again["name"]),
                         ("5", "4", "stalk", "celery"))

    def test_grocery_list_uses_the_upper_bound(self):
        name, quantity, unit, note = groceries.ingredient_parts(
            {"quantity": "4-5", "unit": "stalks", "name": "celery", "original": "4-5 stalks celery"}
        )
        self.assertEqual((name, quantity, unit, note), ("celery", Decimal("5"), "stalk", ""))
        parsed = groceries.parse_line("4-5 stalks celery")
        self.assertEqual((parsed["name"], parsed["quantity"], parsed["unit"]),
                         ("celery", Decimal("5"), "stalk"))

    def test_scaling_data_includes_both_ends(self):
        stored = {"quantity": "4-5", "unit": "stalks", "name": "celery", "original": "4-5 stalks celery"}
        row = meal_view.ingredient_rows([stored])[0]
        self.assertEqual(row["qty"], 5)
        self.assertEqual(row["qty_min"], 4)
        self.assertEqual(row["unit_one"], "stalk")
        self.assertEqual(row["unit_many"], "stalks")


class RepairIngredientRangesTests(TestCase):
    def setUp(self):
        self.user = with_apps(User.objects.create_user("nate", password="pw-not-real-12345"), ["meals"])
        self.space = kitchen(self.user, name="Repair kitchen")

    def _recipe(self, **fields):
        # Bypass Recipe.save's normalize so we can store the pre-fix shape, then repair it.
        recipe = Recipe(space=self.space, created_by=self.user, title="Papa Dave's Dressing", **fields)
        super(Recipe, recipe).save()
        return recipe

    def test_dry_run_prints_counts_and_writes_nothing(self):
        ident = "fedcba9876543210"
        recipe = self._recipe(ingredients=[{
            "id": ident, "quantity": "4-5", "unit": "stalks", "name": "celery",
            "original": "4-5 stalks celery",
        }])
        out = StringIO()
        call_command("repair_ingredient_ranges", "--dry-run", stdout=out)
        self.assertIn("Dry run — would update 1 ingredient(s) on 1 recipe(s). No changes made.", out.getvalue())
        recipe.refresh_from_db()
        self.assertEqual(recipe.ingredients[0]["quantity"], "4-5")
        self.assertEqual(recipe.ingredients[0]["id"], ident)

    def test_apply_keeps_ids_and_is_idempotent(self):
        ident = "fedcba9876543210"
        recipe = self._recipe(ingredients=[{
            "id": ident, "quantity": "4-5", "unit": "stalks", "name": "celery",
            "original": "4-5 stalks celery",
        }])
        out = StringIO()
        call_command("repair_ingredient_ranges", stdout=out)
        self.assertIn("Updated 1 ingredient(s) on 1 recipe(s).", out.getvalue())
        recipe.refresh_from_db()
        row = recipe.ingredients[0]
        self.assertEqual(row["id"], ident)
        self.assertEqual((row["quantity"], row["quantity_min"], row["unit"], row["name"]),
                         ("5", "4", "stalk", "celery"))
        again = StringIO()
        call_command("repair_ingredient_ranges", stdout=again)
        self.assertIn("Nothing to update.", again.getvalue())
        recipe.refresh_from_db()
        self.assertEqual(recipe.ingredients[0]["id"], ident)
