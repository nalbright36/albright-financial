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


@register.filter
def notes_markdown(text):
    """An item's notes (Markdown) on its view panel: rendered and sanitized exactly like a note - no scripts,
    iframes, event handlers or javascript: links; external links go through the link check. Checkboxes show,
    read-only."""
    from albright_notes.render import render
    return render(text or "")


@register.simple_tag(takes_context=True)
def planner_zone(context):
    """The viewer's Planner zone for base.html's auto-detect: {"name": "America/New_York", "auto": True} (no settings
    row yet: the defaults - nothing is written on a page view)."""
    from albright_planner.models import DEFAULT_TIMEZONE, PlannerSettings
    request = context.get("request")
    user = getattr(request, "user", None)
    row = None
    if user is not None and user.is_authenticated:
        row = PlannerSettings.objects.filter(user=user).values("timezone", "auto_timezone").first()
    if row is None:
        return {"name": DEFAULT_TIMEZONE, "auto": True}
    return {"name": row["timezone"], "auto": row["auto_timezone"]}
