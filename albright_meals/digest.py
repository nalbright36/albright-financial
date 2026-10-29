"""Meals' line in the owner's daily Telegram digest
(albright_reselling_app.scanner.alerts.build_digest_message): "Tonight: <dinner>",
when Meals > This week > "My daily digest" is on (the default). The digest
goes only to the owner's Telegram chat - Meals sends nothing to anyone else."""
from html import escape

from .models import MealsSettings
from .plan import tonight


def digest_lines(now=None):
    from django.utils import timezone
    settings_row = MealsSettings.objects.filter(pk=MealsSettings.SINGLETON_PK).first()
    if settings_row is not None and not settings_row.digest_tonight:
        return []
    today = timezone.localdate(now) if now else timezone.localdate()
    dinner = tonight(today)
    return ["", f"Tonight: {escape(dinner.display_title) if dinner else 'nothing planned'}"]
