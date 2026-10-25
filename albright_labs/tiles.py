"""Home's live numbers - one function per tile, each fetched by its own
request (hub.js -> views.tile) so a slow or failing one (e.g. Alpaca down)
never holds up or breaks the others. Each returns {"value": str, "note": str}
or raises TileUnavailable with a short, safe message."""
from decimal import Decimal, InvalidOperation

from django.utils import timezone


class TileUnavailable(Exception):
    """A tile's number can't be shown right now - the message is shown instead."""


def _money(value):
    sign = "-" if value < 0 else "+"
    return f"{sign}${abs(value):,.2f}"


def trade(user):
    """Today's account P&L: equity minus yesterday's closing equity (Alpaca)."""
    from alpaca.trading.client import TradingClient

    from albright_trading_app.models import AlpacaCredentials
    try:
        credentials = user.alpaca_credentials
    except AlpacaCredentials.DoesNotExist:
        raise TileUnavailable("Connect your Alpaca account") from None
    try:
        client = TradingClient(api_key=credentials.get_api_key(), secret_key=credentials.get_secret_key(),
                               paper=credentials.is_paper)
        account = client.get_account()
        equity, last = Decimal(str(account.equity)), Decimal(str(account.last_equity))
    except (InvalidOperation, TypeError, ValueError):
        raise TileUnavailable("Alpaca sent no balance") from None
    except Exception:  # noqa: BLE001 - network, revoked keys, Alpaca outage: show a note, not an error page
        raise TileUnavailable("Couldn't reach Alpaca") from None
    change = equity - last
    pct = f" ({change / last * 100:+.2f}%)" if last else ""
    return {"value": _money(change), "note": f"today{pct} · {'paper' if credentials.is_paper else 'live'}",
            "tone": "gain" if change > 0 else ("loss" if change < 0 else "")}


def resell(user):
    """Items to list, and listed items with a markdown due."""
    from albright_reselling_app.selling import board_columns
    board = board_columns(user, today=timezone.localdate())
    to_list = len(board["to_list"])
    markdowns = sum(1 for card in board["listed"] if card.get("step_due"))
    return {"value": str(to_list + markdowns),
            "note": f"{to_list} to list · {markdowns} markdown{'s' if markdowns != 1 else ''} due"}


def opps(user):
    """What's waiting in the Opportunities Inbox (and how much arrived today)."""
    from albright_crypto_app.inbox_views import inbox_queryset
    inbox = inbox_queryset()
    today = inbox.filter(discovered_at__date=timezone.localdate()).count()
    return {"value": str(inbox.count()), "note": f"in the Inbox · {today} new today"}


def admin(user):
    """Scheduled jobs that are late or failing (Admin > System health)."""
    from albright_control import jobs
    rows = jobs.health_rows()
    failing = sum(1 for r in rows if r["status"] == "failing")
    late = sum(1 for r in rows if r["status"] == "late")
    return {"value": str(failing + late), "note": "all jobs on time" if not (failing or late)
            else f"{failing} failing · {late} late", "tone": "loss" if failing else ("warn" if late else "gain")}


# key -> (label, description, function, superusers only, landing URL name)
TILES = {
    "trade": ("Trade", "Algorithmic trading on Alpaca - strategies, positions, the Grok bot.", trade, False,
              "trading_home"),
    "resell": ("Resell", "Auction scanning, the watchlist, and the resale ledger.", resell, False,
               "albright_reselling_app:dashboard"),
    "opps": ("Opps", "Jobs, freelance work, bounties and crypto - triaged in one Inbox.", opps, False,
             "albright_crypto_app:inbox"),
    "admin": ("Admin", "System health, integrations, AI spend, ideas and the audit log.", admin, True,
              "albright_control:overview"),
}
ACCENTS = {"trade": "trading", "resell": "reselling", "opps": "opportunities", "admin": "admin"}
