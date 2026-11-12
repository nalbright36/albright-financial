"""A per-user "Show weather" toggle (PlannerSettings.show_weather, default on).
When off, Planner pages hide every forecast and skip the Open-Meteo fetches."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("albright_planner", "0018_weather_auto"),
    ]

    operations = [
        migrations.AddField(
            model_name="plannersettings",
            name="show_weather",
            field=models.BooleanField(default=True),
        ),
    ]
