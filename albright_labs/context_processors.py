"""The header's app-switch buttons (templates/index.html): Home, then the
apps this user can open and has kept in their navigation, in their order.
At MAX_BUTTONS or more buttons (Home included) the extras go into a small
"More" menu so the row still fits a phone. Also the "Hidden from your nav"
note for a page of an app the user has hidden."""
from albright_trading_app.context_processors import app_key_for

from . import app_access, apps_registry

MAX_BUTTONS = 5   # Home + 4 apps or more: Home + 3 apps + "More"
INLINE_WHEN_COLLAPSED = MAX_BUTTONS - 2


def app_nav(request):
    user = getattr(request, "user", None)
    resolved = getattr(request, "resolver_match", None)
    current = app_key_for(resolved.app_name if resolved else None)
    if user is None or not user.is_authenticated:
        # Visitors (the sign-in page): the apps anyone can sign in to, as always.
        apps = [app for app in apps_registry.APPS if not app.superuser_only]
        return {"nav_apps": [app_access.nav_item(app, current) for app in apps], "nav_more": [],
                "nav_hidden_app": None}
    pref = app_access.preference(request)
    items = [app_access.nav_item(apps_registry.BY_KEY[key], current) for key in app_access.nav_keys(user, pref)]
    inline, more = items, []
    if len(items) + 1 >= MAX_BUTTONS:
        inline, more = items[:INLINE_WHEN_COLLAPSED], items[INLINE_WHEN_COLLAPSED:]
    hidden = None
    page_app = app_access.app_for_request(request)
    if page_app and page_app.key not in {item["key"] for item in items} \
            and app_access.can_use(user, pref, page_app.key):
        hidden = page_app
    return {"nav_apps": inline, "nav_more": more, "nav_more_current": any(item["current"] for item in more),
            "nav_hidden_app": hidden}
