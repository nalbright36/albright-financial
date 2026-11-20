"""Item pages whose item is gone: one shared wrapper for every item detail / edit page (Planner items, notes, recipes,
workouts, programs, opportunities, money rows, Visuals ...), applied where the URL is declared:

    path("item/<int:pk>/", item_page(views.view_item), name="item")

If the view raises Http404 or ObjectDoesNotExist - the item was deleted, or the person can no longer see it - a page
request goes to that app's home page (apps_registry landing_url_name, found from the URL's namespace) with "That item
no longer exists." instead of an error page. Posts and JSON / fetch requests still get their 404.
"""
import functools

from django.contrib import messages
from django.core.exceptions import ObjectDoesNotExist
from django.http import Http404
from django.shortcuts import redirect
from django.urls import NoReverseMatch, reverse

from . import apps_registry

MESSAGE = "That item no longer exists."


def _wants_json(request):
    accept = request.headers.get("Accept", "")
    return request.headers.get("X-Requested-With") == "XMLHttpRequest" or (
        "application/json" in accept and "text/html" not in accept)


def home_for(request, fallback="labs:hub"):
    """The home page of the app this request's URL belongs to (Home when it's no app's)."""
    match = getattr(request, "resolver_match", None)
    app = apps_registry.app_for_namespace(match.namespace) if match and match.namespace else None
    try:
        return reverse(app.landing_url_name) if app else reverse(fallback)
    except NoReverseMatch:
        return reverse("labs:hub")


def item_page(view=None, *, fallback="labs:hub"):
    """Wrap an item page: a missing / no-longer-visible item sends a page request to the app's home with a note."""
    def wrap(func):
        @functools.wraps(func)
        def wrapper(request, *args, **kwargs):
            try:
                return func(request, *args, **kwargs)
            except (Http404, ObjectDoesNotExist):
                if request.method not in ("GET", "HEAD") or _wants_json(request):
                    raise                              # a form post / fetch: the 404 stays (scripts handle it)
                messages.info(request, MESSAGE)
                return redirect(home_for(request, fallback))
        return wrapper
    return wrap(view) if view is not None else wrap
