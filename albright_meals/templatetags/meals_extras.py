from django import template

from ..groceries import format_quantity

register = template.Library()


@register.filter
def qty(value):
    """2.500 -> "2.5", 3.000 -> "3"."""
    return format_quantity(value)
