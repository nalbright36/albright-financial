"""{% load review_list_tags %}

{{ text|rich_text }} - plain text as HTML for Admin > Ideas / Bot suggestions:
runs of "- " (or "* ") lines become a <ul>, other lines are paragraphs with
their line breaks kept. Everything is escaped; empty text gives "" (so
|default still works after it). review_list.js's richText() does the same
for review mode's card."""
import re

from django import template
from django.utils.html import escape, format_html, format_html_join
from django.utils.safestring import mark_safe

register = template.Library()
BULLET = re.compile(r"^\s*[-*]\s+(.*)$")


def blocks(text):
    """[("ul", [item, ...]) | ("p", [line, ...])] - blank lines end a block."""
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        bullet = BULLET.match(line)
        kind = "ul" if bullet else "p" if line.strip() else None
        if kind is None:
            out.append(None)
            continue
        value = bullet.group(1) if bullet else line
        if out and out[-1] and out[-1][0] == kind:
            out[-1][1].append(value)
        else:
            out.append((kind, [value]))
    return [b for b in out if b]


@register.filter
def rich_text(text):
    parts = []
    for kind, lines in blocks(text):
        if kind == "ul":
            parts.append(format_html("<ul>{}</ul>", format_html_join("", "<li>{}</li>", ((line,) for line in lines))))
        else:
            parts.append("<p>" + "<br>".join(escape(line) for line in lines) + "</p>")
    return mark_safe("".join(parts))
