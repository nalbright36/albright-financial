"""The Household moves here from albright_money, so any app can use it - same rows, same ids, nothing copied.

1. Take over Money's two tables in Django's state only (the tables already exist - no SQL).
2. Rename them to this app's names (albright_money_household -> albright_labs_household, and the members
   table). PostgreSQL keeps every foreign key pointing at the renamed tables, so Money's accounts, categories,
   rules and budgets stay linked to their households.
3. Invitations can name a household to join on sign-up.

albright_money 0002 then drops the two models from Money's state and points its foreign keys here (no SQL).
Reversing runs the rename backwards."""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("albright_labs", "0006_today_show_completed"),
        ("albright_money", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.CreateModel(
                    name="Household",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("name", models.CharField(max_length=100)),
                        ("created_at", models.DateTimeField(auto_now_add=True)),
                        ("created_by", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                                         related_name="+", to=settings.AUTH_USER_MODEL)),
                    ],
                    options={"db_table": "albright_money_household"},
                ),
                migrations.CreateModel(
                    name="HouseholdMember",
                    fields=[
                        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                        ("joined_at", models.DateTimeField(auto_now_add=True)),
                        ("household", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="members",
                                                        to="albright_labs.household")),
                        ("user", models.OneToOneField(on_delete=django.db.models.deletion.CASCADE,
                                                      related_name="household_membership", to=settings.AUTH_USER_MODEL)),
                    ],
                    options={"ordering": ["joined_at", "pk"], "db_table": "albright_money_householdmember"},
                ),
            ],
            database_operations=[],
        ),
        # The tables get this app's names (a rename - the rows and every foreign key to them stay).
        migrations.AlterModelTable(name="household", table=None),
        migrations.AlterModelTable(name="householdmember", table=None),
        migrations.AddField(
            model_name="invitation",
            name="household",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="invitations", to="albright_labs.household"),
        ),
    ]
