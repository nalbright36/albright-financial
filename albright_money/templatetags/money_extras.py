"""Money formatting for Albright Money's templates: usd ("$1,234.56", "−$12.00") and signed ("+$1,200.00",
"−$42.18" - a transaction's direction)."""
from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()
MINUS = "\u2212"


def _value(value):
    try:
        return Decimal(value or 0)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(0)


@register.filter
def usd(value):
    v = _value(value)
    return f"{MINUS if v < 0 else ''}${abs(v):,.2f}"


@register.filter
def usd0(value):
    v = _value(value)
    return f"{MINUS if v < 0 else ''}${abs(v):,.0f}"


@register.filter
def signed(value):
    v = _value(value)
    return f"{'+' if v > 0 else MINUS if v < 0 else ''}${abs(v):,.2f}"


@register.filter
def scope_of(obj):
    """"shared" or "personal" - which category menu a transaction's row uses."""
    return "shared" if getattr(obj, "household_id", None) else "personal"
