import re
from decimal import Decimal

import django.core.validators
from django.db import migrations, models

TIMES = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*×\s*(.+)$")


def backfill(apps, schema_editor):
    """Every entry keeps its numbers; it just learns its amount so it can be edited: a USDA portion ("2 × 1 cup" ->
    2 of "1 cup"), else its grams; My foods and recipes their servings. Quick adds stay as they are."""
    Entry = apps.get_model("albright_fitness", "FoodLogEntry")
    for e in Entry.objects.exclude(source="quick").iterator():
        quantity, unit = None, ""
        if e.source == "usda":
            match = TIMES.match(e.portion_label or "")
            if match:
                quantity, unit = Decimal(match.group(1)), "p:" + match.group(2)
            elif e.portion_label:
                quantity, unit = Decimal(1), "p:" + e.portion_label
            elif e.grams:
                quantity, unit = e.grams, "g"
        elif e.source in ("custom", "recipe"):
            quantity, unit = e.servings or Decimal(1), "serving"
        if quantity:
            Entry.objects.filter(pk=e.pk).update(quantity=quantity, unit=unit[:64])


class Migration(migrations.Migration):

    dependencies = [("albright_fitness", "0011_program_setup")]

    operations = [
        migrations.AddField("customfood", "serving_ml", models.DecimalField(blank=True, decimal_places=1, max_digits=7, null=True,
                                                                            validators=[django.core.validators.MinValueValidator(0)])),
        migrations.AddField("foodlogentry", "quantity", models.DecimalField(blank=True, decimal_places=3, max_digits=9, null=True)),
        migrations.AddField("foodlogentry", "unit", models.CharField(blank=True, max_length=64)),
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
