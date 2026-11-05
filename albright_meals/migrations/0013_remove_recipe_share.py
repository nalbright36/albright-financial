"""Recipes' shares move to the one Share system (schema): the old RecipeShare table is gone - ShareGrant has its rows (0012_shares_to_grants)."""
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ("albright_meals", "0012_shares_to_grants"),
    ]

    operations = [
        migrations.DeleteModel(name="RecipeShare"),
    ]
