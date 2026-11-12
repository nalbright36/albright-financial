"""Saved places on the Weather app (Home and Current location stay on PlannerSettings)."""
from django.conf import settings
from django.db import models


class WeatherPlace(models.Model):
    """A place the user saved on the Weather page (label + coordinates, their order)."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="weather_places")
    label = models.CharField(max_length=200)
    lat = models.DecimalField(max_digits=8, decimal_places=5)
    lon = models.DecimalField(max_digits=8, decimal_places=5)
    sort = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("sort", "pk")

    def __str__(self):
        return self.label
