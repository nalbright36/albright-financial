"""Meals' line in the owner's daily Telegram digest
(albright_reselling_app.scanner.alerts.build_digest_message): "Tonight: <dinner>",
when Meals > This week > "My daily digest" is on (the default) - the owner's space's dinner. The digest
goes only to the owner's Telegram chat - Meals sends nothing to anyone else."""
from html import escape

from .models import MealsSettings
from .plan import tonight


def owner_space():
    """The digest goes to the site owner (the first superuser): the Meals space they last looked at."""
    from django.contrib.auth import get_user_model

    from albright_labs.circles import current_space
    owner = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
    return current_space(owner, "meals") if owner else None


def digest_lines(now=None):
    from django.utils import timezone
    space = owner_space()
    if space is None:
        return []
    settings_row = MealsSettings.objects.filter(space=space).first()
    if settings_row is not None and not settings_row.digest_tonight:
        return []
    today = timezone.localdate(now) if now else timezone.localdate()
    dinner = tonight(space, today)
    return ["", f"Tonight: {escape(dinner.display_title) if dinner else 'nothing planned'}"]
