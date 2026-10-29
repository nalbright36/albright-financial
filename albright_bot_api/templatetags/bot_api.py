"""{% load bot_api %} - shows what the bot left, on the pages it belongs to:
  {% bot_trading_notes user %}            Grok Bot page, "Bot notes" (newest first)
  {% bot_lot_notes lot_id %}              a lot's expandable row, labeled "Grok"
  {% bot_inventory_suggestions entry %}   the item's page: pending suggestions to accept/dismiss
  {% bot_opportunity_notes opp %}         an opportunity's page: Grok notes and proposal drafts
All bot text is plain text - autoescaped, never marked safe."""
from django import template

from ..models import InventorySuggestion, LotNote, OpportunityNote, TradingNote

register = template.Library()
TRADING_NOTES_SHOWN = 20


@register.inclusion_tag("albright_bot_api/_trading_notes.html", takes_context=True)
def bot_trading_notes(context, user, strategy=None):
    """All the user's notes (strategy=None), one strategy's (a BotStrategy), or the untagged ones ("untagged")."""
    if not getattr(user, "is_authenticated", False):
        return {"notes": [], "csrf_token": context.get("csrf_token"), "strategy": strategy}
    notes = TradingNote.objects.filter(owner=user).select_related("strategy", "token")
    if strategy == "untagged":
        notes = notes.filter(strategy=None)
    elif strategy is not None:
        notes = notes.filter(strategy=strategy)
    notes = notes.order_by("-date", "-created_at", "-pk")[:TRADING_NOTES_SHOWN]
    return {"notes": notes, "csrf_token": context.get("csrf_token"), "strategy": strategy}


@register.inclusion_tag("albright_bot_api/_lot_notes.html")
def bot_lot_notes(lot_id):
    return {"notes": LotNote.objects.filter(lot_id=lot_id).order_by("-created_at", "-pk")[:5]}


@register.inclusion_tag("albright_bot_api/_inventory_suggestions.html", takes_context=True)
def bot_inventory_suggestions(context, entry):
    suggestions = InventorySuggestion.objects.filter(entry=entry)
    return {"pending": suggestions.filter(status="pending"), "decided": suggestions.exclude(status="pending")[:5],
            "csrf_token": context.get("csrf_token")}


@register.inclusion_tag("albright_bot_api/_opportunity_notes.html")
def bot_opportunity_notes(opportunity):
    return {"notes": OpportunityNote.objects.filter(opportunity=opportunity).order_by("-created_at", "-pk")}
