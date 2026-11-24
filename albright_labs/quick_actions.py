"""Home's quick-add popups: an action tile (home_tiles, kind=ACTION with action="<name>") opens one right on Home.

hub.js fetches the popup (labs:quick_action -> templates/albright_labs/quick/<name>.html) and shows it in a centered
dialog. Its form posts straight to the app's own save view with the header X-Quick-Add: 1 - the same validation and
save code as the app's own pages; the view answers JSON (albright_labs/quick.py) instead of redirecting. hub.js then
closes the popup, shows a toast ("Task added · View") and refreshes the Today panel and tile badges (labs:hub_refresh).

Adding one: a function below that returns the popup's context (only what it needs - fields, defaults, the save URL),
an entry in ACTIONS, and its template. "More details" (more_url) opens the app's full form; the popup's fields with
data-more="<param>" ride along as ?param=value (the full form reads them as initial values).
"""
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.template.loader import render_to_string
from django.urls import reverse

from . import app_access, home_tiles


def _today(user):
    from albright_planner import planner
    return planner.today(user)


def _share_options(user, app):
    """The New forms' Share choice (sharing.apply_posted share_mode): the default, Only me, or one circle."""
    from . import sharing
    return ([("default", f"Default: {sharing.default_text(user, app)}"), ("me", "Only me")]
            + [(f"circle:{c.pk}", c.name) for c in sharing.shareable_circles(user, app)])


def _planner(request, kind):
    from albright_planner.recurrence import REPEATS
    simple = ("none", "daily", "weekdays", "weekly", "monthly_day", "yearly")     # no extra fields needed
    return {"kind": kind, "today": _today(request.user), "post_url": reverse("albright_planner:new"),
            "more_url": reverse("albright_planner:new") + ("?kind=event" if kind == "event" else ""),
            "repeats": [(k, label.replace(" on chosen days", "").replace(" on day X", "")) for k, label in REPEATS if k in simple],
            "share": _share_options(request.user, "planner")}


def task(request):
    return _planner(request, "task")


def event(request):
    return _planner(request, "event")


def _meals_space(user):
    from .circles import current_space
    return current_space(user, "meals")


def recipe(request):
    return {"import_url": reverse("albright_meals:recipe_import"), "post_url": reverse("albright_meals:recipe_add"),
            "more_url": reverse("albright_meals:recipe_add"), "share": _share_options(request.user, "meals")}


def groceries(request):
    from albright_meals import scope
    space = _meals_space(request.user)
    return {"post_url": reverse("albright_meals:groceries"), "more_url": reverse("albright_meals:groceries"),
            "list_name": scope.title(space) if space and space.circle_id else "", "space": space}


def plan_meal(request):
    from albright_meals import scope
    from albright_meals.models import SLOTS
    space = _meals_space(request.user)
    return {"post_url": reverse("albright_meals:plan_add"), "more_url": reverse("albright_meals:week"), "space": space,
            "recipes": scope.own(space).only("pk", "title").order_by("title") if space else [], "slots": SLOTS,
            "today": _today(request.user)}


def food(request):
    from albright_assistant import access

    from albright_fitness.food_find_views import slot_now
    return {"search_url": reverse("albright_fitness:food_find"), "more_url": reverse("albright_fitness:food_find"),
            "slot": slot_now(user=request.user), "today": _today(request.user), "ai": access.allowed(request.user)}


def weight(request):
    from albright_fitness import body
    from albright_fitness.body_views import WEIGHT_LIMITS
    from albright_fitness.models import NutritionProfile, WeightEntry
    profile = NutritionProfile.load(request.user)
    last = WeightEntry.objects.filter(owner=request.user).order_by("-date").first()
    unit = profile.weight_unit
    return {"post_url": reverse("albright_fitness:log_weight"), "more_url": reverse("albright_fitness:body"),
            "unit": unit, "units": list(WEIGHT_LIMITS), "low": WEIGHT_LIMITS[unit][0], "high": WEIGHT_LIMITS[unit][1],
            "last": body.shown(last.weight_kg, unit) if last else None, "last_date": last.date if last else None,
            "today": _today(request.user)}


def workout(request):
    return {"post_url": reverse("albright_fitness:quick"), "more_url": reverse("albright_fitness:dashboard")}


def note(request):
    return {"post_url": reverse("albright_notes:capture"), "more_url": reverse("albright_notes:list") + "?start=new"}


def checklist(request):
    return {"post_url": reverse("albright_notes:capture"), "more_url": reverse("albright_notes:list")}


def transaction(request):
    from albright_money.views import _accounts
    return {"post_url": reverse("albright_money:quick_add"), "more_url": reverse("albright_money:transactions"),
            "accounts": _accounts(request.user).filter(archived=False).order_by("name"), "today": _today(request.user)}


def opportunity(request):
    from albright_crypto_app.forms import OPPORTUNITY_TYPES
    return {"post_url": reverse("albright_crypto_app:add"), "more_url": reverse("albright_crypto_app:add"),
            "types": OPPORTUNITY_TYPES}


def idea(request):
    from albright_control.models import Idea
    return {"post_url": reverse("albright_control:idea_add"), "more_url": reverse("albright_control:idea_add"),
            "categories": Idea._meta.get_field("category").choices}


# name: (popup title, context function). The template is quick/<name>.html.
ACTIONS = {
    "task": ("Add task", task), "event": ("Add event", event),
    "recipe": ("Add recipe", recipe), "groceries": ("Add groceries", groceries), "plan_meal": ("Plan a meal", plan_meal),
    "food": ("Log food", food), "weight": ("Log weight", weight), "workout": ("Quick log workout", workout),
    "note": ("New note", note), "checklist": ("Quick checklist", checklist),
    "transaction": ("Add transaction", transaction), "opportunity": ("Add opportunity", opportunity),
    "idea": ("Submit an idea", idea),
}


def tile_for(user, name):
    """The action tile for this popup, if this person may use it (its app open to them, not archived) - else None."""
    tile = next((t for t in home_tiles.catalog(user).values() if t.action == name), None)
    return tile if tile and home_tiles.can_add(user, tile.key) else None


def popup(request, name):
    """GET: the popup's HTML (hub.js puts it in the dialog). 404 for an unknown action or one the person can't use."""
    from django.contrib.auth.views import redirect_to_login
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    tile = tile_for(request.user, name) if name in ACTIONS else None
    if tile is None:
        raise Http404
    title, context_for = ACTIONS[name]
    return render(request, f"albright_labs/quick/{name}.html",
                  {"title": title, "tile": tile, "name": name, **context_for(request)})


def refresh(request):
    """After a quick add: Home's Today panel (HTML, or "" without one) and the tile badges, counted afresh."""
    from django.contrib.auth.views import redirect_to_login
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path())
    from . import landing
    pref = app_access.preference(request)
    home_tiles.forget_badges(request.user)
    mine = home_tiles.user_tiles(request.user, pref)
    today_html = ""
    if pref.show_today_panel and app_access.can_use(request.user, pref, "planner"):
        try:
            from albright_planner.planner import today_panel
            panel = {**today_panel(request.user, show_completed=pref.show_completed_today), "here": landing.home_url()}
            today_html = render_to_string("albright_planner/_today_panel.html", {"planner_today": panel}, request=request)
        except Exception:  # noqa: BLE001 - the page keeps the panel it has
            today_html = ""
    return JsonResponse({"today_html": today_html, "badges": home_tiles.badges(request.user, mine)})
