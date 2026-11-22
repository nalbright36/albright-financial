from django.apps import AppConfig


class AlbrightNotesConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "albright_notes"
    verbose_name = "Albright Notes"

    def ready(self):
        from . import signals  # noqa: F401  (smart links: Planner tasks and grocery lines write back to notes)
