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


@register.simple_tag
def term(key):
    """A small "?" that explains a term in plain words (glossary.TERMS): tap it (fitness.js) for a short note - a
    popover, or a card at the bottom of a phone screen; the note is also its title for hover."""
    from django.utils.html import format_html
    from .. import glossary
    label, text = glossary.get(key)
    return format_html('<span class="fx-term"><button type="button" class="fx-term__btn" aria-expanded="false" '
                       'aria-label="What is {}?" title="{}: {}" data-term>?</button><span class="fx-term__body" role="note" '
                       'hidden><strong>{}</strong> {}</span></span>', label, label, text, label, text)
