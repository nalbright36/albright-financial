from django import template
from django.contrib.humanize.templatetags.humanize import intcomma

register = template.Library()


@register.filter
def money(value):
    """$1,234.56 with thousands separators, or "-" for a zero/blank
    value - keeps every money cell in ledger.html formatted identically."""
    if not value:
        return "-"
    value = float(value)
    sign = "-" if value < 0 else ""
    return f"{sign}${intcomma('%.2f' % abs(value))}"
