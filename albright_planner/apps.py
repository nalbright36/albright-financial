from django.apps import AppConfig


class AlbrightPlannerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "albright_planner"
    verbose_name = "Albright Planner"

    def ready(self):
        from . import signals  # noqa: F401 - grocery lines in task checklists
        from albright_labs.request_cache import watch

        from .models import PlannerSettings
        watch(PlannerSettings)   # PlannerSettings.row / for_user are read once per request
