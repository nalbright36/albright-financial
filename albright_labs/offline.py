"""Offline viewing (read-only), the server's half. The browser's half: /sw.js (albright_labs/templates/albright_labs/sw.js),
labs/offline.js on every page and labs/offline_view.js on the offline page.

  - OfflineMiddleware tells the service worker about every HTML page with an X-Albright-Offline header:
      "anon"            nobody is signed in      -> the worker wipes every saved page and the snapshot
      "off"             offline data is off      -> the same (My account > Offline data)
      "nocache <id>"    signed in, don't keep this page (sign-in, Admin, API keys, the assistant, a no-store reply)
      "cache <id>"      signed in, keep it       -> saved for when the device is offline
    A different <id> from the one the worker saved for means someone else signed in: it wipes first.
    Django's never_cache (sent by Home, Fitness and others so the browser's Back never shows a stale page) is not a
    "don't keep" - the worker always asks the network first and shows the copy only offline. Any other no-store is.
    It also stamps a cookie (CHANGED_COOKIE) on every successful change, so offline.js refreshes the snapshot.
  - snapshot(): /offline/snapshot/, the signed-in person's key data as JSON, kept in IndexedDB so the offline page
    can show Today, Week, Groceries, Recipes and Notes even for pages never opened.
  - settings(): My account's on/off switch.
"""
import logging
import time
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import app_access

log = logging.getLogger(__name__)

HEADER = "X-Albright-Offline"
CHANGED_COOKIE = "albright_changed"
# Never kept for offline: signing in and out, Admin, API keys, the AI assistant, the Bot API, password links.
NEVER = ("/user_login", "/user_logout", "/admin/", "/control/", "/account/", "/assistant/", "/bot-api/", "/api/",
         "/oauth", "/.well-known/", "/reset", "/offline/", "/sw.js", "/invite", "/signup")
NOTES_MAX, NOTE_CHARS, RECIPES_MAX, WEIGHTS = 300, 20000, 200, 14


def enabled(user):
    from .models import UserPreference
    return bool(user and user.is_authenticated and UserPreference.for_user(user).offline_enabled)


def _never_cache_reply(cc):
    """Django's never_cache ("max-age=0, no-cache, no-store, must-revalidate, private") - not a privacy no-store."""
    return "max-age=0" in cc and "must-revalidate" in cc


def tag(request, response):
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return "anon"
    if not enabled(user):
        return "off"
    cc = response.get("Cache-Control", "").lower()
    keep = (response.status_code == 200 and not request.path.startswith(NEVER)
            and not ("no-store" in cc and not _never_cache_reply(cc)))
    return f"{'cache' if keep else 'nocache'} {user.pk}"


class OfflineMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.method == "GET":
            if response.get("Content-Type", "").startswith("text/html"):
                response[HEADER] = tag(request, response)
        elif response.status_code < 400 and getattr(request, "user", None) is not None and request.user.is_authenticated:
            # Something changed: offline.js sees a newer stamp than its snapshot's and fetches a fresh one.
            response.set_cookie(CHANGED_COOKIE, str(int(time.time() * 1000)), max_age=60 * 60 * 24, samesite="Lax",
                                secure=request.is_secure(), httponly=False)
        return response


def context(request):
    """index.html's <body data-offline-...>: who the saved data belongs to, and whether to keep it."""
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"offline_on": False}
    return {"offline_on": enabled(user)}


# ---------------------------------------------------------------------------
# The snapshot
# ---------------------------------------------------------------------------

def _hm(t):
    return t.strftime("%H:%M") if t else None


def _planner(user):
    from albright_planner import checklist, planner
    start = planner.today(user)
    end = start + timedelta(days=7)
    rows = []
    for o in checklist.attach(planner.occurrences(user, start, end)):
        rows.append({"date": o.date.isoformat(), "title": o.title, "start": _hm(o.start_time), "end": _hm(o.end_time),
                     "kind": o.item.kind, "checkable": o.item.checkable, "done": bool(o.done),
                     "skipped": bool(getattr(o, "skipped", False)),
                     "checklist": [{"text": r.text, "done": bool(r.checked)} for r in getattr(o, "checklist", [])]})
    return {"start": start.isoformat(), "end": end.isoformat(), "items": rows}


def _ingredient(i):
    if isinstance(i, str):
        return i
    if i.get("original"):
        return i["original"]
    head = " ".join(str(p) for p in (i.get("quantity"), i.get("unit"), i.get("name")) if p)
    return f"{head}, {i['note']}" if i.get("note") else head


def _meals(user):
    from albright_labs import circles
    from albright_meals import scope as meals_scope
    from albright_meals.groceries import format_quantity
    from albright_meals.models import GroceryItem, MealPlanEntry
    from albright_meals.plan import week_start
    space = circles.current_space(user, "meals")
    if space is None:
        return {}
    groceries = [{"name": i.name, "quantity": format_quantity(i.quantity) or "", "unit": i.unit, "aisle": i.aisle,
                  "note": i.note, "checked": i.checked}
                 for i in GroceryItem.objects.active().filter(space=space)]
    start = week_start()
    end = start + timedelta(days=6)
    plan = [{"date": e.date.isoformat(), "slot": e.get_slot_display() if hasattr(e, "get_slot_display") else e.slot,
             "title": e.display_title, "recipe_id": e.recipe_id}
            for e in MealPlanEntry.objects.filter(space=space, date__gte=start, date__lte=end).select_related("recipe")]
    recipes = list(meals_scope.own(space).order_by("-updated_at", "-pk")[:RECIPES_MAX])
    stamps = meals_scope.stamps(recipes, space, user)
    rows = []
    for r in recipes:
        mine = [s for s in stamps.get(r.pk, []) if s.user_id == user.pk] or stamps.get(r.pk, [])
        rows.append({"id": r.pk, "title": r.title, "description": r.description,
                     "ingredients": [_ingredient(i) for i in (r.ingredients or [])], "steps": list(r.steps or []),
                     "rating": mine[0].get_level_display() if mine else None,
                     "rating_level": mine[0].level if mine else None,
                     "servings": r.servings, "minutes": (r.prep_minutes or 0) + (r.cook_minutes or 0) or None})
    return {"space": space.name, "groceries": groceries,
            "meal_plan": {"start": start.isoformat(), "end": end.isoformat(), "entries": plan}, "recipes": rows}


def _notes(user):
    from albright_notes.models import Note
    notes = Note.objects.filter(owner=user, archived=False).order_by("-pinned", "-updated_at", "-pk")[:NOTES_MAX]
    return [{"id": n.pk, "title": n.title, "body": (n.body or "")[:NOTE_CHARS], "pinned": n.pinned, "tags": n.tags,
             "updated_at": n.updated_at.isoformat()} for n in notes]


def _fitness(user):
    from albright_fitness import body, nutrition
    from albright_fitness.models import FoodLogEntry, NutritionProfile, WeightEntry
    day = nutrition.today(user)
    entries = FoodLogEntry.objects.filter(owner=user, date=day).order_by("created_at", "pk")
    food = [{"slot": e.get_slot_display(), "label": e.label, "calories": nutrition.whole(e.calories),
             "protein": nutrition.whole(e.protein)} for e in entries]
    unit = NutritionProfile.load(user).weight_unit
    weights = [{"date": w.date.isoformat(), "weight": f"{body.shown(w.weight_kg, unit)}"}
               for w in WeightEntry.objects.filter(owner=user).order_by("-date")[:WEIGHTS]]
    return {"food": {"date": day.isoformat(), "entries": food,
                     "calories": sum(f["calories"] or 0 for f in food), "protein": sum(f["protein"] or 0 for f in food)},
            "weights": {"unit": unit, "entries": weights}}


def build(user, pref):
    """{"user", "saved_at", ...} - each app only for someone who can open it; one broken part never sinks the rest."""
    data = {"user": user.pk, "saved_at": timezone.now().isoformat(), "planner": None, "groceries": None,
            "meal_plan": None, "recipes": None, "notes": None, "food": None, "weights": None}
    parts = (("planner", _planner), ("meals", _meals), ("notes", _notes), ("fitness", _fitness))
    for key, fn in parts:
        if not app_access.can_use(user, pref, key):
            continue
        try:
            out = fn(user)
        except Exception:  # noqa: BLE001 - the snapshot is a convenience; log it and send the rest
            log.exception("Offline snapshot: %s failed", key)
            continue
        if key == "planner":
            data["planner"] = out
        elif key == "notes":
            data["notes"] = out
        else:
            data.update(out)
    return data


@login_required
@require_GET
@never_cache
def snapshot(request):
    if not enabled(request.user):
        return JsonResponse({"ok": False, "off": True})
    return JsonResponse({"ok": True, **build(request.user, app_access.preference(request))})


@login_required
@require_POST
def settings(request):
    """My account > Offline data: on (default) or off. Off: every device wipes its copy on the next page."""
    from .models import UserPreference
    pref = UserPreference.objects.get_or_create(user=request.user)[0]
    pref.offline_enabled = request.POST.get("enabled") == "on"
    pref.save(update_fields=["offline_enabled", "updated_at"])
    messages.success(request, "Offline data is on." if pref.offline_enabled
                     else "Offline data is off - this device's copy is cleared.")
    return redirect(reverse("labs:account") + "#offline-data")
