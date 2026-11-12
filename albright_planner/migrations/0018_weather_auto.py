"""Follow this device's location for Planner weather (PlannerSettings.weather_auto),
falling back to the typed home. The last GPS fix is stored separately so the
typed home is kept as the fallback."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("albright_planner", "0017_weather_locations"),
    ]

    operations = [
        migrations.AddField(
            model_name="plannersettings",
            name="weather_auto",
            field=models.BooleanField(default=True),
        ),
        migrations.AddField(
            model_name="plannersettings",
            name="weather_auto_label",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="plannersettings",
            name="weather_auto_lat",
            field=models.DecimalField(blank=True, decimal_places=5, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name="plannersettings",
            name="weather_auto_lon",
            field=models.DecimalField(blank=True, decimal_places=5, max_digits=8, null=True),
        ),
    ]
