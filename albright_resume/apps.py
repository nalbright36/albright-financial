from django.apps import AppConfig


class AlbrightResumeConfig(AppConfig):
    """Public interactive resume + employer chatbot.

    Deliberately isolated: nothing in this app imports the Trading, Resell,
    Opportunities, Bot API or Admin models, and the chatbot only ever sees the
    content stored in this app's own tables (see chat.build_knowledge)."""
    default_auto_field = "django.db.models.BigAutoField"
    name = "albright_resume"
    verbose_name = "Public resume"
