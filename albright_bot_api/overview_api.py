"""Bot API v1 - read helpers across apps (/api/bot/v1/...): one note's text, one recipe, a search over the owner's
items, and today's summary. Built for the in-site AI assistant (albright_assistant) but ordinary API operations:
they're in catalog.OPS, so API keys, the OpenAPI spec and the MCP tools get them too.

  GET notes/<id>             getNote          notes:read   - one note the owner can see (theirs or shared with them), with its Markdown body
  GET meals/recipes/<id>     getRecipe        meals:read   - one recipe the owner's Meals space can see
  GET search?q=              searchMyItems    any of notes:read, meals:read, schedule:read - only the parts it may read
  GET search/all?q=&app=     searchEverything any of schedule:read, notes:read, meals:read, fitness:read - the site's
                                              global search (albright_notes.links.find), only the groups it may read
  GET today                  getTodaySummary  any of schedule:read, meals:read, fitness:read - likewise

Every check is the usual one (auth.bot_endpoint): the key's scopes AND the owner's app access right now."""
from .auth import ApiError, bot_endpoint
from . import scopes as scope_rules

SEARCH_LIMIT = 10
NOTE_BODY_MAX = 20_000          # enough to summarize; the rest is cut (truncated: true)


def _usable(token, scope):
    return scope_rules.usable(token, scope)


@bot_endpoint("notes:read")
def note_detail(request, token, note_id):
    from albright_notes.models import Note
    from .notes_api import _note_json, _require_notes_access
    _require_notes_access(token)
    note = Note.objects.visible_to(token.owner).select_related("owner").filter(pk=note_id).first()
    if note is None:
        raise ApiError(404, f"No note {note_id} the owner can see.", "rejected: note not found")
    body = note.body or ""
    from albright_notes import smart
    out = {**_note_json(note, request, viewer=token.owner), "body": body[:NOTE_BODY_MAX], "truncated": len(body) > NOTE_BODY_MAX,
           "due_items": smart.due_items(body), "grocery_items": smart.grocery_items(body)}
    return out, 200, f"read note #{note.pk}"


@bot_endpoint("meals:read")
def recipe_detail(request, token, recipe_id):
    from albright_meals import scope as meals_scope
    from .meals_api import _recipe_json, _space
    space = _space(token, request)
    recipe = meals_scope.readable(token.owner, space).filter(pk=recipe_id).select_related("created_by").first()
    if recipe is None:
        raise ApiError(404, f"No recipe {recipe_id} in this space.", "rejected: recipe not found")
    stamps = meals_scope.stamps([recipe], space, token.owner)
    return _recipe_json(recipe, space, stamps), 200, f"read recipe #{recipe.pk}"


def _query(request):
    q = (request.GET.get("q") or "").strip()[:100]
    if not q:
        raise ApiError(400, '"q" is required.', "rejected: missing q")
    return q


@bot_endpoint(("notes:read", "meals:read", "schedule:read"))
def search(request, token):
    q = _query(request)
    owner = token.owner
    out = {"q": q}
    if _usable(token, "notes:read"):
        from albright_notes import notes
        from albright_notes.models import Note
        found = notes.search(Note.objects.visible_to(owner).filter(archived=False), q)[:SEARCH_LIMIT]
        out["notes"] = [{"id": n.pk, "title": n.title, "tags": n.tags} for n in found]
    if _usable(token, "meals:read"):
        from albright_labs import circles
        from albright_meals import scope as meals_scope
        from albright_meals.models import GroceryItem
        space = circles.current_space(owner, "meals", None)
        if space is not None:
            out["recipes"] = [{"id": r.pk, "title": r.title} for r in
                              meals_scope.own(space).filter(title__icontains=q).order_by("-updated_at")[:SEARCH_LIMIT]]
            out["groceries"] = [{"id": g.pk, "name": g.name, "checked": g.checked} for g in
                                GroceryItem.objects.active().filter(space=space, name__icontains=q)[:SEARCH_LIMIT]]
    if _usable(token, "schedule:read"):
        from albright_planner.models import ScheduleItem
        items = ScheduleItem.objects.visible_to(owner).filter(title__icontains=q, archived=False) \
            .order_by("-pk")[:SEARCH_LIMIT]
        out["planner_items"] = [{"id": i.pk, "title": i.title, "kind": i.kind,
                                 "date": i.date.isoformat()}
                                for i in items]
    count = sum(len(v) for v in out.values() if isinstance(v, list))
    return out, 200, f"searched {q[:40]!r}: {count} found"


# The global search's groups each scope may read. Money, Ideas and People have no Bot API scope, so a key never
# searches them.
SEARCH_GROUPS = {"schedule:read": "planner", "notes:read": "notes", "meals:read": "recipes", "fitness:read": "fitness"}


@bot_endpoint(tuple(SEARCH_GROUPS))
def search_all(request, token):
    from albright_notes import links
    q = _query(request)
    only = {group for scope, group in SEARCH_GROUPS.items() if _usable(token, scope)}
    group = request.GET.get("app", "")
    if group and group not in only:
        raise ApiError(400, f'"app" must be one of: {", ".join(sorted(only))}.', "rejected: bad app")
    groups = links.find(token.owner, q, group or None, only=only)
    out = {"q": q, "groups": [{"key": g["key"], "label": g["label"], "more": g["more"], "results": [
        {k: r[k] for k in ("type", "id", "title", "snippet", "info", "url")} for r in g["results"]]} for g in groups]}
    count = sum(len(g["results"]) for g in groups)
    return out, 200, f"searched everything {q[:40]!r}: {count} found"


@bot_endpoint(("schedule:read", "meals:read", "fitness:read"))
def today(request, token):
    from albright_planner import planner
    on = planner.today(token.owner)
    owner = token.owner
    out = {"date": on.isoformat(), "weekday": on.strftime("%A")}
    if _usable(token, "schedule:read"):
        from albright_planner import checklist
        from .schedule_api import _occurrence_json
        occs = checklist.attach(planner.occurrences(owner, on, on, localize=False))
        out["planner"] = [_occurrence_json(o, {}) for o in occs]
    if _usable(token, "meals:read"):
        from albright_labs import circles
        from albright_meals.models import MealPlanEntry
        from .meals_api import _entry_json
        space = circles.current_space(owner, "meals", None)
        if space is not None:
            out["meals"] = [_entry_json(e) for e in MealPlanEntry.objects.filter(space=space, date=on)
                            .select_related("recipe", "created_by")]
    if _usable(token, "fitness:read"):
        from albright_fitness import nutrition, stats
        from albright_fitness.models import MACROS, FoodLogEntry, NutritionProfile
        profile = NutritionProfile.load(owner)
        eaten = nutrition.totals(FoodLogEntry.objects.filter(owner=owner, date=on))
        out["fitness"] = {
            "planned_workouts": [{"name": d.name, "program": d.program.name, "done": bool(w)}
                                 for d, w in stats.todays(owner, on)],
            "macros": {n: {"eaten": nutrition.whole(eaten[n]), "target": profile.target(n)} for n in MACROS},
        }
    return out, 200, f"read today {on}"
