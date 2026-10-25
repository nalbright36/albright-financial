from django.apps import AppConfig


class AlbrightLabsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "albright_labs"
    verbose_name = "Albright Labs"

    def ready(self):
        from . import checks  # noqa: F401 - registers the static-files check
