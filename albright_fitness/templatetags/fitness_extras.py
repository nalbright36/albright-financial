"""Formatting for Albright Fitness's templates."""
from django import template

from .. import training

register = template.Library()


@register.filter
def weight(value):
    """225.00 -> "225"."""
    return training.fmt_weight(value)


@register.filter
def clock(seconds):
    """1710 -> "28:30"."""
    return training.clock(seconds) if seconds else ""


@register.filter
def days(weekdays):
    """[0, 2, 4] -> "Mon · Wed · Fri"."""
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return " · ".join(names[d] for d in sorted(weekdays or []) if 0 <= d <= 6)


@register.filter
def get_item(mapping, key):
    return mapping.get(key, key) if mapping else key
