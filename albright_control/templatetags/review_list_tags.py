"""{% load review_list_tags %}

{{ text|rich_text }} - plain text as HTML for Admin > Ideas / Bot suggestions:
- runs of "- " / "* " lines become a <ul>;
- runs of numbered lines - "(1) ...", "1. ...", "1) ..." - become an <ol>;
- a paragraph that numbers its steps inline, "Change: (1) Add X. (2) Add Y.",
  becomes its lead-in plus an <ol> of the steps (only when they count 1, 2, 3...);
- other lines are paragraphs, their line breaks kept.
Everything is escaped; empty text gives "" (so |default still works after
it). Review mode's card gets the same HTML (review_list.Kind.row)."""
import re

from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()
BULLET = re.compile(r"^\s*[-*•]\s+(.*)$")
NUMBERED = re.compile(r"^\s*(?:\(\d{1,2}\)|\d{1,2}[.)])\s+(.*)$")
INLINE_STEP = re.compile(r"(?:^|\s)\((\d{1,2})\)\s+")


def blocks(text):
    """[(kind, [line, ...])] with kind "ul", "ol" or "p" - a blank line ends a block."""
    out = []
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        bullet, numbered = BULLET.match(line), NUMBERED.match(line)
        kind = "ul" if bullet else "ol" if numbered else "p" if line.strip() else None
        if kind is None:
            out.append(None)
            continue
        value = (bullet or numbered).group(1) if (bullet or numbered) else line
        if out and out[-1] and out[-1][0] == kind:
            out[-1][1].append(value)
        else:
            out.append((kind, [value]))
    return [b for b in out if b]


def inline_steps(paragraph):
    """("lead-in", [step, ...], "after") when the paragraph numbers 2+ steps (1), (2), ... in order, else None."""
    marks = list(INLINE_STEP.finditer(paragraph))
    if len(marks) < 2 or [int(m.group(1)) for m in marks] != list(range(1, len(marks) + 1)):
        return None
    steps = [paragraph[m.end():(marks[i + 1].start() if i + 1 < len(marks) else len(paragraph))].strip()
             for i, m in enumerate(marks)]
    return paragraph[:marks[0].start()].strip(), steps


def _list(tag, items):
    return f"<{tag}>" + "".join(f"<li>{escape(item)}</li>" for item in items) + f"</{tag}>"


@register.filter
def rich_text(text):
    parts = []
    for kind, lines in blocks(text):
        if kind in ("ul", "ol"):
            parts.append(_list(kind, lines))
            continue
        split = inline_steps(" ".join(line.strip() for line in lines)) if len(lines) == 1 else None
        if split:
            lead, steps = split
            if lead:
                parts.append(f"<p>{escape(lead)}</p>")
            parts.append(_list("ol", steps))
        else:
            parts.append("<p>" + "<br>".join(escape(line) for line in lines) + "</p>")
    return mark_safe("".join(parts))
