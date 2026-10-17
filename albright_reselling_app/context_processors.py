"""Template context for the reselling app's shared nav (templates/index.html)."""
from django.utils.functional import SimpleLazyObject

RESELLER_APP_NAME = "albright_reselling_app"


def watchlist_nav(request):
    """The nav's Watchlist count badge (scanner/dashboard.watchlist_nav_summary).
    Lazy, so the query only runs when a page actually renders the nav -
    never for the many partials/JSON views that also go through render() -
    and only inside the reselling app for a logged-in user."""
    resolved = request.resolver_match
    user = getattr(request, "user", None)
    if resolved is None or resolved.app_name != RESELLER_APP_NAME or not (user and user.is_authenticated):
        return {}

    def summary():
        from .scanner.dashboard import watchlist_nav_summary
        return watchlist_nav_summary()

    return {"watchlist_nav": SimpleLazyObject(summary)}
