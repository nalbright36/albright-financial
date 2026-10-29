"""The header's app launcher (templates/index.html): which tiles it shows,
and the one short live line under each - fetched as JSON (views.launcher_lines)
only after the panel opens, so opening it never waits on Alpaca or the DB.

Each app's line is computed on its own: one that fails (Alpaca down, no
credentials) is just None - an empty line on its tile - and never stops the
others. The answer is cached per user for CACHE_SECONDS.
"""
import logging

from django.core.cache import cache
from django.utils import timezone

from . import app_access, apps_registry, tiles

log = logging.getLogger(__name__)

CACHE_SECONDS = 60
HOME_KEY = "home"
HOME_LINE = "Overview"
MAX_DINNER = 28


def launcher_items(user, pref, current_accent):
    """Home first, then the apps this user can open and shows, in their order - Admin
    (superusers only, since only they can open it) always last."""
    keys = app_access.nav_keys(user, pref)
    keys = [key for key in keys if key != "admin"] + [key for key in keys if key == "admin"]
    home = {"key": HOME_KEY, "short_label": "Home", "label": "Home", "brand": apps_registry.HOME_BRAND,
            "accent": apps_registry.HOME_ACCENT, "color": "", "url_name": "labs:hub",
            "current": current_accent == apps_registry.HOME_ACCENT, "line": HOME_LINE}
    return [home] + [app_access.nav_item(apps_registry.BY_KEY[key], current_accent) for key in keys]


def shows_launcher(user, pref):
    """No launcher for someone who can open just one app (or none) - there's nothing to switch to."""
    return len(app_access.allowed_keys(user, pref)) > 1


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def trade(user):
    return f"{tiles.trade(user)['value']} today"


def resell(user):
    from albright_reselling_app.selling import board_columns
    board = board_columns(user, today=timezone.localdate())
    to_list = len(board["to_list"])
    markdowns = sum(1 for card in board["listed"] if card.get("step_due"))
    parts = ([f"{to_list} to list"] if to_list else []) + ([f"{_plural(markdowns, 'markdown')} due"] if markdowns else [])
    return " · ".join(parts) or "Nothing to list"


def opps(user):
    from albright_crypto_app.inbox_views import inbox_queryset
    return f"{inbox_queryset().count()} new in Inbox"


def meals(user):
    from albright_meals.plan import tonight
    dinner = tonight()
    if not dinner:
        return "No dinner planned"
    title = dinner.display_title
    return title if len(title) <= MAX_DINNER else title[:MAX_DINNER - 1].rstrip() + "…"


def admin(user):
    from albright_control import jobs
    problems = sum(1 for row in jobs.health_rows() if row["status"] in ("failing", "late"))
    return _plural(problems, "problem") if problems else "All good"


LINES = {"trade": trade, "resell": resell, "opps": opps, "meals": meals, "admin": admin}


def _line(key, user):
    try:
        return LINES[key](user)
    except tiles.TileUnavailable:
        return None
    except Exception:  # noqa: BLE001 - one app's line failing leaves just that tile's line empty
        log.exception("Launcher line for %s failed", key)
        return None


def lines(user, pref):
    """{app key: short line or None} for every app this user can open (cached per user),
    trimmed to the ones currently in their launcher."""
    cache_key = f"labs:launcher-lines:{user.pk}"
    allowed = app_access.allowed_keys(user, pref)
    values = cache.get(cache_key)
    if values is None or not set(allowed) <= set(values):
        values = {key: _line(key, user) for key in allowed if key in LINES}
        cache.set(cache_key, values, CACHE_SECONDS)
    shown = {item["key"] for item in launcher_items(user, pref, None)}
    return {HOME_KEY: HOME_LINE, **{key: value for key, value in values.items() if key in shown}}
