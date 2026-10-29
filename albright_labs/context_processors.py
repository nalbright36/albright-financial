"""The header's app launcher (templates/index.html): a grid button that opens a
panel of tiles - Home, then the apps this user can open and has kept in their
navigation, in their order, Admin last (albright_labs.launcher). Hidden for
someone who can open only one app. Also the "Hidden from your nav" note for a
page of an app the user has hidden."""
from albright_trading_app.context_processors import app_key_for

from . import app_access, launcher


def app_nav(request):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"launcher_apps": [], "show_launcher": False, "nav_hidden_app": None}
    resolved = getattr(request, "resolver_match", None)
    current = app_key_for(resolved.app_name if resolved else None)
    pref = app_access.preference(request)
    items = launcher.launcher_items(user, pref, current)
    hidden = None
    page_app = app_access.app_for_request(request)
    if page_app and page_app.key not in {item["key"] for item in items} \
            and app_access.can_use(user, pref, page_app.key):
        hidden = page_app
    return {"launcher_apps": items, "show_launcher": launcher.shows_launcher(user, pref), "nav_hidden_app": hidden}
