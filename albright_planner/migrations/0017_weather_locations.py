"""A home location for weather (PlannerSettings) and an optional place on an item
when it isn't at home. Empty: weather is hidden (or the item uses the viewer's home)."""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("albright_planner", "0016_scheduleitem_parent"),
    ]

    operations = [
        migrations.AddField(
            model_name="plannersettings",
            name="weather_label",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="plannersettings",
            name="weather_lat",
            field=models.DecimalField(blank=True, decimal_places=5, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name="plannersettings",
            name="weather_lon",
            field=models.DecimalField(blank=True, decimal_places=5, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name="scheduleitem",
            name="location_label",
            field=models.CharField(blank=True, max_length=200),
        ),
        migrations.AddField(
            model_name="scheduleitem",
            name="location_lat",
            field=models.DecimalField(blank=True, decimal_places=5, max_digits=8, null=True),
        ),
        migrations.AddField(
            model_name="scheduleitem",
            name="location_lon",
            field=models.DecimalField(blank=True, decimal_places=5, max_digits=8, null=True),
        ),
    ]
