"""Planner tasks linked to an Admin Idea (several per Idea; deleting the Idea unlinks them)."""
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("albright_planner", "0013_remove_item_share"),
        ("albright_control", "0003_idea"),
    ]

    operations = [
        migrations.AddField(
            model_name="scheduleitem",
            name="idea",
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                                    related_name="planner_tasks", to="albright_control.idea"),
        ),
    ]
