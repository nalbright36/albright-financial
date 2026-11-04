"""Meals' line in the owner's daily Telegram digest
(albright_reselling_app.scanner.alerts.build_digest_message): "Tonight: <dinner>",
when Meals > This week > "My daily digest" is on (the default) - the owner's household's dinner. The digest
goes only to the owner's Telegram chat - Meals sends nothing to anyone else."""
from html import escape

from .models import MealsSettings
from .plan import tonight


def owner_household():
    """The digest goes to the site owner (the first superuser): their household's plan."""
    from django.contrib.auth import get_user_model

    from albright_labs.models import household_of
    owner = get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("pk").first()
    return household_of(owner) if owner else None


def digest_lines(now=None):
    from django.utils import timezone
    household = owner_household()
    if household is None:
        return []
    settings_row = MealsSettings.objects.filter(household=household).first()
    if settings_row is not None and not settings_row.digest_tonight:
        return []
    today = timezone.localdate(now) if now else timezone.localdate()
    dinner = tonight(household, today)
    return ["", f"Tonight: {escape(dinner.display_title) if dinner else 'nothing planned'}"]
