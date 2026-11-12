from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="WeatherPlace",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("label", models.CharField(max_length=200)),
                ("lat", models.DecimalField(decimal_places=5, max_digits=8)),
                ("lon", models.DecimalField(decimal_places=5, max_digits=8)),
                ("sort", models.PositiveIntegerField(default=0)),
                ("user", models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name="weather_places",
                    to=settings.AUTH_USER_MODEL,
                )),
            ],
            options={"ordering": ("sort", "pk")},
        ),
    ]
