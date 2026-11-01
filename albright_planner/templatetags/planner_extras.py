"""Who an item belongs to, at a glance: the owner's initial in a small colored circle (the same color for the same
person everywhere) and their first name."""
from django import template

from albright_planner.planner import person as _person

register = template.Library()

# Distinct, readable on dark and light (white text).
AVATAR_COLORS = ("#7C3AED", "#DB2777", "#EA580C", "#0891B2", "#16A34A", "#2563EB", "#CA8A04", "#DC2626")


@register.filter
def person(user):
    """"Kim" - the first name, else the username."""
    return _person(user)


@register.filter
def initial(user):
    name = _person(user)
    return name[:1].upper() if name else "?"


@register.filter
def avatar_color(user):
    return AVATAR_COLORS[user.pk % len(AVATAR_COLORS)] if user and user.pk else AVATAR_COLORS[0]
