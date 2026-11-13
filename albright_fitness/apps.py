from django.apps import AppConfig


class AlbrightFitnessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "albright_fitness"
    verbose_name = "Albright Fitness"

    def ready(self):
        from . import schedule  # noqa: F401 - its post_delete receiver (a schedule's Planner task goes with it)
