"""Where a signed-in user lands: after signing in (unless a "next" URL was
given) and on "/". The choice is per user (models.UserPreference):

  - Home (default): the Labs hub
  - Last used app: from the last_app cookie (albright_trading_proj.middleware)
  - A specific page: one of LANDING_PAGES below - each app's main nav pages
"""
from django.urls import NoReverseMatch, reverse

from albright_trading_proj.middleware import LAST_APP_COOKIE, OPPORTUNITIES_COOKIE_VALUES

from .models import UserPreference

# (key, group, label, URL name, superusers only) - the main nav pages of each app.
LANDING_PAGES = [
    ("trade:home", "Trade", "Home", "trading_home", False),
    ("trade:account", "Trade", "Trading Account", "trading_account", False),
    ("trade:grok_bot", "Trade", "Grok Bot", "grok_bot", False),
    ("trade:scanner", "Trade", "Market Scanner", "market_scanner", False),
    ("trade:strategies", "Trade", "Strategies", "strategies", False),
    ("resell:today", "Resell", "Today", "albright_reselling_app:today", False),
    ("resell:dashboard", "Resell", "Dashboard", "albright_reselling_app:dashboard", False),
    ("resell:watchlist", "Resell", "Watchlist", "albright_reselling_app:watchlist", False),
    ("resell:review", "Resell", "Review", "albright_reselling_app:review_queue", False),
    ("resell:ledger", "Resell", "Ledger", "albright_reselling_app:ledger", False),
    ("resell:calculator", "Resell", "Calculator", "albright_reselling_app:calculator", False),
    ("opps:inbox", "Opps", "Inbox", "albright_crypto_app:inbox", False),
    ("opps:pipeline", "Opps", "Pipeline", "albright_crypto_app:pipeline", False),
    ("opps:earnings", "Opps", "Earnings", "albright_crypto_app:earnings", False),
    ("admin:overview", "Admin", "Overview", "albright_control:overview", True),
    ("admin:ideas", "Admin", "Ideas", "albright_control:ideas", True),
    ("admin:health", "Admin", "System health", "albright_control:health", True),
]
PAGES = {key: (group, label, url_name, superuser_only) for key, group, label, url_name, superuser_only in LANDING_PAGES}

# The last_app cookie's values -> that app's landing page.
LAST_APP_TARGETS = {"trading": "trading_home", "reseller": "albright_reselling_app:dashboard",
                    "opportunities": "albright_crypto_app:inbox", "admin": "albright_control:overview"}


def page_choices(user):
    """[(group, [(key, label), ...]), ...] for the dropdown - Admin pages for superusers only."""
    groups = {}
    for key, group, label, _url, superuser_only in LANDING_PAGES:
        if superuser_only and not user.is_superuser:
            continue
        groups.setdefault(group, []).append((key, label))
    return list(groups.items())


def allowed_page(user, key):
    page = PAGES.get(key)
    return page is not None and (not page[3] or user.is_superuser)


def _reverse(url_name):
    try:
        return reverse(url_name)
    except NoReverseMatch:
        return None


def landing_url(request):
    """The URL to send this signed-in user to, or None for Home (the hub)."""
    pref = UserPreference.for_user(request.user)
    if pref.landing == "page" and allowed_page(request.user, pref.landing_page):
        return _reverse(PAGES[pref.landing_page][2])
    if pref.landing == "last_app":
        remembered = request.COOKIES.get(LAST_APP_COOKIE)
        if remembered in OPPORTUNITIES_COOKIE_VALUES:
            remembered = "opportunities"   # the old "crypto" value
        if remembered == "admin" and not request.user.is_superuser:
            return None
        target = LAST_APP_TARGETS.get(remembered)
        return _reverse(target) if target else None
    return None
