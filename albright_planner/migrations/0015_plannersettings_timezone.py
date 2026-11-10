"""Each person's Planner time zone (follows the browser unless auto_timezone is off). Items keep their own
ScheduleItem.timezone; nothing about existing items changes."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("albright_planner", "0014_scheduleitem_idea"),
    ]

    operations = [
        migrations.AddField(
            model_name="plannersettings",
            name="timezone",
            field=models.CharField(default="America/New_York", max_length=64),
        ),
        migrations.AddField(
            model_name="plannersettings",
            name="auto_timezone",
            field=models.BooleanField(default=True),
        ),
    ]
