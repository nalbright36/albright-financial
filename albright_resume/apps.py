from django.apps import AppConfig


class AlbrightResumeConfig(AppConfig):
    """RETIRED - the public resume (/resume/) and its employer chatbot were removed (/resume/ now answers 410 Gone:
    albright_trading_proj.urls). Only this shell is left: its migrations (0004 drops the tables) and
    `manage.py export_resume_data`, which saves the rows to ~/retired_app_exports/ - run it before applying 0004.
    Once 0004 has been applied everywhere, this app can be taken out of INSTALLED_APPS and deleted."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "albright_resume"
    verbose_name = "Public resume (retired)"
