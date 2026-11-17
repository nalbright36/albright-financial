from django.apps import AppConfig


class AlbrightFitnessConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "albright_fitness"
    verbose_name = "Albright Fitness"

    def ready(self):
        from . import schedule, weigh_in  # noqa: F401 - their post_delete receivers
