"""{% load grok_bot_extras %} - the Grok Bot pages' number formatting.

  {{ value|signed_money }}   +$1,234.50 / -$50.00 / an em dash for None
  {{ value|signed_pct }}     +12.50% / -3.10% / an em dash for None
  {{ value|pnl_class }}      gain / loss / neutral (for .badge and colored numbers)
  {{ names|get_item:key }}   a StrategyRef by its key
"""
from decimal import Decimal

from django import template
from django.contrib.humanize.templatetags.humanize import intcomma

register = template.Library()
DASH = "—"


@register.filter
def signed_money(value):
    if value is None or value == "":
        return DASH
    value = Decimal(value).quantize(Decimal("0.01"))
    return f"{'-' if value < 0 else '+'}${intcomma(f'{abs(value):.2f}')}"


@register.filter
def signed_pct(value):
    if value is None or value == "":
        return DASH
    value = Decimal(value)
    return f"{'-' if value < 0 else '+'}{abs(value):.2f}%"


@register.filter
def pnl_class(value):
    if value is None or value == "":
        return "neutral"
    return "gain" if value > 0 else "loss" if value < 0 else "neutral"


@register.filter
def get_item(mapping, key):
    """{{ names|get_item:key }} - a strategy by its key (Unattributed for an unknown one)."""
    from ..strategy_attribution import UNATTRIBUTED_REF
    return (mapping or {}).get(key) or UNATTRIBUTED_REF
