"""The meal plan: weeks run Monday to Sunday; one meal per day per slot.
Moving a meal onto a taken slot swaps the two."""
from datetime import date, timedelta

from django.db import transaction
from django.utils import timezone

from .models import SLOTS, MealPlanEntry

SLOT_KEYS = [key for key, _label in SLOTS]
_MOVING = "_moving"   # a slot value no real entry uses, held for a moment while two entries swap


def week_start(day=None):
    day = day or timezone.localdate()
    return day - timedelta(days=day.weekday())


def parse_day(value, default=None):
    try:
        return date.fromisoformat(value) if value else default
    except (TypeError, ValueError):
        return default


def week_days(start):
    return [start + timedelta(days=i) for i in range(7)]


def week(start):
    """[(day, {slot: entry})] for the 7 days from start."""
    entries = MealPlanEntry.objects.filter(date__gte=start, date__lt=start + timedelta(days=7)) \
        .select_related("recipe", "created_by")
    by_day = {}
    for entry in entries:
        by_day.setdefault(entry.date, {})[entry.slot] = entry
    return [(day, by_day.get(day, {})) for day in week_days(start)]


def set_slot(day, slot, *, recipe=None, title="", notes=None, user=None, token=None):
    """Puts a meal in a day's slot - replacing whatever was planned there. Returns (entry, created)."""
    defaults = {"recipe": recipe, "title": "" if recipe else title, "status": "planned",
                "from_bot": token is not None, "bot_token": token,
                "created_by": user if user and user.is_authenticated else None}
    if notes is not None:
        defaults["notes"] = notes
    return MealPlanEntry.objects.update_or_create(date=day, slot=slot, defaults=defaults)


def move(entry, day, slot):
    """Moves a meal to another day/slot; a meal already there takes this one's old place."""
    if (entry.date, entry.slot) == (day, slot):
        return None
    with transaction.atomic():
        other = MealPlanEntry.objects.select_for_update().filter(date=day, slot=slot).exclude(pk=entry.pk).first()
        old_day, old_slot = entry.date, entry.slot
        if other:
            entry.slot = _MOVING
            entry.save(update_fields=["slot", "updated_at"])
            other.date, other.slot = old_day, old_slot
            other.save(update_fields=["date", "slot", "updated_at"])
        entry.date, entry.slot = day, slot
        entry.save(update_fields=["date", "slot", "updated_at"])
    return other


def tonight(today=None):
    """Today's dinner, if one is planned (and not skipped)."""
    return MealPlanEntry.objects.filter(date=today or timezone.localdate(), slot="dinner") \
        .exclude(status="skipped").select_related("recipe").first()
