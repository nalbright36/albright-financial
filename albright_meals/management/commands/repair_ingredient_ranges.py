"""Re-parse stored recipe ingredients so ranges like quantity "4-5" keep their numbers.

Existing rows were often saved as {"quantity": "4-5", "unit": "stalks", "name": "celery"}.
parse_quantity("4-5") is not a number, so the page showed just "stalks" and the grocery
list dropped the amount. This command runs the same normalize_list Recipe.save already
uses, keeps each line's stable id (so ingredient photos stay attached), and is idempotent.

  python manage.py repair_ingredient_ranges --dry-run
  python manage.py repair_ingredient_ranges

--dry-run prints counts only and writes nothing. Running it again after a fix prints
"Nothing to update".
"""
from django.core.management.base import BaseCommand

from albright_meals.ingredients import normalize_list
from albright_meals.models import Recipe


def _sig(row):
    if not isinstance(row, dict):
        return ("raw", str(row))
    return (row.get("id"), str(row.get("quantity") or ""), str(row.get("quantity_min") or ""),
            str(row.get("unit") or ""), str(row.get("name") or ""))


def _changed_count(before, after):
    n = abs(len(before or []) - len(after or []))
    for old, new in zip(before or [], after or []):
        if _sig(old) != _sig(new):
            n += 1
    return n


class Command(BaseCommand):
    help = "Re-parse ingredient ranges on every recipe. Keeps ingredient ids. Idempotent."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Print counts only; do not write.")

    def handle(self, *args, **options):
        dry = options["dry_run"]
        recipes_n = 0
        ings_n = 0
        for recipe in Recipe.objects.iterator():
            before = list(recipe.ingredients or [])
            after = normalize_list(before)
            n = _changed_count(before, after)
            if not n:
                continue
            recipes_n += 1
            ings_n += n
            if not dry:
                recipe.ingredients = after
                recipe.save(update_fields=["ingredients"])
        if not recipes_n:
            self.stdout.write("Nothing to update.")
            return
        if dry:
            self.stdout.write(
                f"Dry run — would update {ings_n} ingredient(s) on {recipes_n} recipe(s). No changes made."
            )
        else:
            self.stdout.write(f"Updated {ings_n} ingredient(s) on {recipes_n} recipe(s).")
