"""Workouts' shares move to the one Share system (schema): the old WorkoutShare table is gone - ShareGrant has its rows (0005_shares_to_grants)."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("albright_fitness", "0005_shares_to_grants"),
    ]

    operations = [
        migrations.DeleteModel(name="WorkoutShare"),
    ]
