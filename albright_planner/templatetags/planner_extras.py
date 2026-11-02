"""Who an item belongs to, at a glance: the owner's initial in a small colored circle (the same color for the same
person everywhere) and their first name."""
import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

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


LIST_LINE = re.compile(r"^(?:([-*\u2022])|\d+[.)])\s+(.*)$")


@register.filter
def notes_html(text):
    """An item's notes on its view panel: paragraphs keep their line breaks, and "- " / "* " / "1. " lines become
    lists. Everything is escaped first."""
    out, para, items, kind = [], [], [], None
    for raw in (text or "").splitlines() + [""]:
        line = raw.strip()
        match = LIST_LINE.match(line)
        want = ("ul" if match.group(1) else "ol") if match else None
        if para and (match or not line):
            out.append("<p>" + "<br>".join(para) + "</p>")
            para = []
        if items and want != kind:
            out.append(f"<{kind}>" + "".join(f"<li>{i}</li>" for i in items) + f"</{kind}>")
            items = []
        if match:
            kind = want
            items.append(escape(match.group(2)))
        elif line:
            para.append(escape(line))
    return mark_safe("".join(out))
