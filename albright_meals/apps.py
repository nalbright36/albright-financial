from django.apps import AppConfig


class AlbrightMealsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "albright_meals"
    verbose_name = "Albright Meals"

    def ready(self):
        from . import signals
        signals.connect()               # activity in circle kitchens (albright_labs.activity)
