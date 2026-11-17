"""Eat out pages (/meals/eat-out/): the filters and ranked list (eatout.py), "We went here" (the household's rating,
RestaurantVisit, in the space being shown), named filter presets, the last-used filters, and the warning page for
external links. A down Overpass or geocode API never fails the page - it says "unavailable" (with the cause for
admins), never a fake "no matches"."""
import json
import re
from datetime import date
from urllib.parse import urlencode

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, QueryDict
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from albright_labs.links import is_web_url, link_status, outbound_href

from . import eatout, scope
from .models import EatOutPreset, EatOutSettings, RecipeRating, RestaurantVisit

PLACE_ID_RE = re.compile(r"^osm:(node|way|relation)/\d{1,15}$")
MAX_PRESETS = 12
LEAVE = "albright_meals:eat_out_leave"


def _page_url(query=""):
    """The Eat out page with a query string that came back from a form - only ever a query string."""
    query = (query or "").lstrip("?")
    if not query or "://" in query or "\n" in query or query.startswith("/"):
        return reverse("albright_meals:eat_out")
    return f"{reverse('albright_meals:eat_out')}?{query}"


def _card(place):
    """A Place plus its outbound links (albright_labs.links.outbound_href - an unverified site gets the warning
    page first)."""
    href = lambda url: outbound_href(url, leave=LEAVE) if url else ""  # noqa: E731
    return {"p": place, "directions": href(place.directions_url), "map": href(place.map_url),
            "website": href(place.website), "menu": href(place.menu), "reserve": href(place.reserve),
            "price": "$" * place.price if place.price else "",
            "anchor": "eo-" + re.sub(r"[^a-z0-9]+", "-", place.place_id.lower()).strip("-")}


def _filters_for(request):
    """(filters, the params they came from). A submitted form (f=1) is remembered as the last-used filters; a bare
    visit opens with them."""
    get = request.GET
    if eatout.has_filters(get):
        filters = eatout.filters_from_get(get)
        if get.get("f") == "1":
            EatOutSettings.objects.update_or_create(user=request.user,
                                                    defaults={"last_query": eatout.query_string(filters)})
        return filters, get
    saved = EatOutSettings.objects.filter(user=request.user).values_list("last_query", flat=True).first()
    if saved:
        source = QueryDict(saved)
        return eatout.filters_from_get(source), source
    return eatout.DEFAULTS, get


@login_required
@scope.space_required
def eat_out(request):
    """Filters on the left (More filters folded on a phone), the ranked list on the right."""
    filters, source = _filters_for(request)
    loc, loc_error, home, current, redirect_to = eatout.resolve_location(request.user, request.GET)
    if redirect_to:
        place = eatout.Location(redirect_to["label"], redirect_to["lat"], redirect_to["lon"], "typed")
        return redirect(_page_url(eatout.query_string(filters, place)))
    result = eatout.search(loc, filters, request.user, request.space)
    raw_pick = request.GET.get("pick", "")
    if raw_pick == "random":
        pick = eatout.random_pick(result.places)
    else:
        pick = int(raw_pick) if raw_pick.isdigit() and 1 <= int(raw_pick) <= len(result.places) else 0
    presets = list(EatOutPreset.objects.filter(user=request.user))
    current_query = eatout.query_string(filters)
    return render(request, "albright_meals/eatout.html", {
        "filters": filters,
        "field_errors": eatout.filter_errors(source),
        "loc": loc, "loc_error": loc_error, "loc_caption": eatout.source_caption(loc.source) if loc else "",
        "home": home, "current": current,
        "cards": [_card(p) for p in result.places],
        "result": result,
        "summary": eatout.summary_text(filters),
        "pick": pick,
        "qs": eatout.query_string(filters, loc),
        "pick_qs": eatout.query_string(filters, loc, "random"),
        "any_cuisine_qs": eatout.query_string(filters._replace(cuisines=()), loc),
        "more_open": eatout.more_active(filters),
        "presets": [{"preset": p, "on": p.query == current_query} for p in presets],
        "preset_query": current_query,
        "loc_qs": urlencode(eatout.location_params(loc)),
        "types": eatout.TYPES, "cuisines": [(k, label) for k, label, _t in eatout.CUISINES],
        "prices": eatout.PRICES, "miles": eatout.MILES, "whens": eatout.WHENS, "days": list(enumerate(eatout.DAYS_FULL)),
        "meals": eatout.MEALS, "features": [(k, label) for k, label, _c in eatout.FEATURES],
        "services": eatout.SERVICES, "seens": eatout.SEENS, "sorts": eatout.SORTS,
        "levels": [{"value": v, "label": label, "emoji": RecipeRating.INFO[v][0]} for v, label in RestaurantVisit.LEVELS],
        "today": timezone.localdate(),
        "weather_auto": eatout.weather_follows_device(request.user),
        "location_url": reverse("albright_meals:eat_out_location"),
    })


@login_required
@require_POST
def eat_out_location(request):
    """The browser's rough coordinates, saved onto PlannerSettings.weather_auto_* the same way Planner weather
    already does. Lives under /meals/ so a Meals-only account can reach it."""
    try:
        body = json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        body = {}
    body = body if isinstance(body, dict) else {}
    payload, status = eatout.set_current_location(request.user, body.get("lat"), body.get("lon"))
    return JsonResponse(payload, status=status)


@login_required
@require_POST
@scope.space_required
def eat_out_visit(request):
    """"We went here": the household's rating of a place (one per place in the space - going again updates it)."""
    post = request.POST
    back = _page_url(post.get("next"))
    place_id = (post.get("place_id") or "").strip()
    name = " ".join((post.get("name") or "").split())[:120]
    try:
        level = int(post.get("level") or 0)
    except ValueError:
        level = 0
    try:
        visited = date.fromisoformat(post.get("visited_on") or "") if post.get("visited_on") else timezone.localdate()
    except ValueError:
        visited = None
    if not PLACE_ID_RE.match(place_id) or not name:
        messages.error(request, "That place couldn't be saved - search again and retry.")
        return redirect(back)
    if level not in dict(RestaurantVisit.LEVELS):
        messages.error(request, f"Pick how {name} was.")
        return redirect(back)
    if visited is None or visited > timezone.localdate():
        messages.error(request, "Pick the day you went (not a future date).")
        return redirect(back)
    visit, made = RestaurantVisit.objects.update_or_create(
        space=request.space, place_id=place_id,
        defaults={"name": name, "level": level, "note": (post.get("note") or "").strip()[:500], "visited_on": visited})
    if made:
        visit.created_by = request.user
        visit.save(update_fields=["created_by"])
    messages.success(request, f"Saved {name}: {visit.get_level_display()}.")
    return redirect(back)


@login_required
@require_POST
def eat_out_preset(request):
    """Save the current filters under a name (the same name replaces it)."""
    name = " ".join((request.POST.get("name") or "").split())[:40]
    back = _page_url(request.POST.get("next"))
    if not name:
        messages.error(request, "Give the preset a name, like “Date night”.")
        return redirect(back)
    mine = EatOutPreset.objects.filter(user=request.user)
    if mine.count() >= MAX_PRESETS and not mine.filter(name=name).exists():
        messages.error(request, f"You can keep {MAX_PRESETS} presets - delete one first.")
        return redirect(back)
    query = eatout.query_string(eatout.filters_from_get(QueryDict(request.POST.get("query") or "")))
    EatOutPreset.objects.update_or_create(user=request.user, name=name, defaults={"query": query})
    messages.success(request, f"Saved the preset “{name}”.")
    return redirect(back)


@login_required
@require_POST
def eat_out_preset_delete(request, pk):
    preset = get_object_or_404(EatOutPreset, pk=pk, user=request.user)
    preset.delete()
    messages.success(request, f"Deleted the preset “{preset.name}”.")
    return redirect(_page_url(request.POST.get("next")))


@login_required
def eat_out_leave(request):
    """The warning page an unverified external link (a restaurant's website, directions) goes through."""
    url = request.GET.get("to", "")
    domain, status = link_status(url)
    if status == "allowlisted":
        return redirect(url)
    return render(request, "albright_meals/eatout_leave.html", {
        "url": url if is_web_url(url) else "", "domain": domain, "status": status})
