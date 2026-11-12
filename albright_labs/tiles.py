"""Home's live numbers - one function per tile, each fetched by its own
request (hub.js -> views.tile) so a slow or failing one (e.g. Alpaca down)
never holds up or breaks the others. Each returns {"value": str, "note": str}
or raises TileUnavailable with a short, safe message. Which function is
each app's tile: App.tile in apps_registry."""
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


def meals(user):
    """Tonight's dinner, and what's still on the grocery list - in the Meals space they last used."""
    from albright_meals.models import GroceryItem
    from albright_meals.plan import tonight

    from .circles import current_space
    space = current_space(user, "meals")
    if space is None:
        return {"value": "Nothing planned", "note": "Meals", "size": "text"}
    dinner = tonight(space)
    to_get = GroceryItem.objects.active().filter(space=space, checked=False).count()
    title = dinner.display_title if dinner else "Nothing planned"
    if len(title) > 40:
        title = title[:39].rstrip() + "…"
    return {"value": title, "note": f"tonight · {to_get} on the grocery list", "tone": "" if dinner else "warn",
            "size": "text"}


def notes(user):
    """Pinned notes, and the most recently changed note's title (the user's own and ones shared with them)."""
    from albright_notes.models import Note
    visible = Note.objects.visible_to(user).filter(archived=False)
    pinned = visible.filter(pinned=True).count()
    latest = visible.order_by("-updated_at", "-pk").values_list("title", flat=True).first()
    if latest and len(latest) > 40:
        latest = latest[:39].rstrip() + "…"
    return {"value": latest or "No notes yet", "note": f"{pinned} pinned" + (" · latest" if latest else ""),
            "size": "text", "tone": "" if latest else "warn"}


def planner(user):
    """Today's open tasks (and how many are overdue)."""
    from albright_planner.planner import open_tasks_today
    todays, overdue = open_tasks_today(user)
    count = len(todays) + len(overdue)
    return {"value": str(count), "note": f"open task{'s' if count != 1 else ''} today"
            + (f" · {len(overdue)} overdue" if overdue else ""), "tone": "warn" if overdue else ""}


def money(user):
    """What's left of this month's budget (spending categories, household and personal), and how many
    transactions need a look."""
    from albright_money.budget import left_this_month
    from albright_money.models import Transaction
    left = left_this_month(user)
    review = Transaction.objects.visible_to(user).filter(needs_review=True).count()
    note = f"{review} to review" if review else "nothing to review"
    if left is None:
        return {"value": "No budget", "note": f"this month · {note}", "size": "text", "tone": "warn"}
    return {"value": f"${abs(left):,.0f}", "note": ("left" if left >= 0 else "over") + f" this month · {note}",
            "tone": "" if left >= 0 else "loss"}


def fitness(user):
    """Today's planned workout (or a rest day), and this week's done vs planned."""
    from albright_fitness import stats
    line = stats.status_line(user)
    monday = stats.week_start(timezone.localdate())
    done, planned = stats.done_in_week(user, monday), stats.planned_in_week(user, monday)
    week = f"{done} of {planned} this week" if planned else f"{done} this week"
    return {"value": line.split(": ", 1)[-1] if ": " in line else line, "note": week, "size": "text",
            "tone": "" if planned and done >= planned else ""}


def weather(user):
    """Current temperature and conditions at Home or Current location."""
    from albright_weather.services import tile_line
    return tile_line(user)


def admin(user):
    """Scheduled jobs that are late or failing (Admin > System health)."""
    from albright_control import jobs
    rows = jobs.health_rows()
    failing = sum(1 for r in rows if r["status"] == "failing")
    late = sum(1 for r in rows if r["status"] == "late")
    return {"value": str(failing + late), "note": "all jobs on time" if not (failing or late)
            else f"{failing} failing · {late} late", "tone": "loss" if failing else ("warn" if late else "gain")}

