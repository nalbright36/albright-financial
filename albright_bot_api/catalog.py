"""Every Bot API endpoint, described once. The same list builds:
  - GET /api/bot/v1/docs - the plain-language guide written for an AI (markdown())
  - GET /api/bot/v1/openapi.json - the OpenAPI 3.1 spec (openapi()), and fitness/openapi.json (just the food log)
  - GET /api/bot/v1/me - what a key may call (for_token())
  - the MCP server's tools (mcp.py - tools()), with the same names (operationId) and descriptions
so they never drift. A test fails when a route in urls.api_urlpatterns isn't here.

Nothing here holds user data: the examples are made up.
"""
from dataclasses import dataclass, field
import json
import re

from . import scopes as scope_rules
from .auth import RATE_LIMIT

API_PREFIX = "/api/bot/v1/"


# --- schema shorthands -------------------------------------------------------------------------------------------

def _s(kind, desc="", **extra):
    out = {"type": kind}
    if desc:
        out["description"] = desc
    out.update(extra)
    return out


def S(desc="", **kw):
    return _s("string", desc, **kw)


def I(desc="", **kw):
    return _s("integer", desc, **kw)


def N(desc="", **kw):
    return _s("number", desc, **kw)


def B(desc="", **kw):
    return _s("boolean", desc, **kw)


def D(desc="YYYY-MM-DD", **kw):
    return _s("string", desc, format="date", **kw)


def A(items, desc="", **kw):
    return _s("array", desc, items=items, **kw)


def O(desc="", props=None, **kw):
    out = _s("object", desc, **kw)
    if props:
        out["properties"] = props
    return out


ENUM = lambda values, desc="": S(desc, enum=list(values))
SHARE = O('Optional: {"circle": <circle id>, "permission": "view"|"edit"} or {"user": "<username or email>", '
          '"permission": "view"|"edit"}')
SLOT = ENUM(["breakfast", "lunch", "dinner", "snack"])
SPACE = I("A Meals space id from meals/spaces (default: the one the owner last picked)")
NUM_OR_TEXT = {"type": ["number", "string"]}


def _item_props():
    num = lambda high, desc="": N(desc, minimum=0, maximum=high)
    return {
        "name": S("What it is, e.g. Chipotle chicken burrito bowl", maxLength=200),
        "quantity": N("How many units", exclusiveMinimum=0, maximum=5000),
        "unit": S("Free text: bowl, slice, cup, g ...", maxLength=40),
        "grams": num(5000),
        "calories": num(5000), "protein_g": num(1000), "carbs_g": num(1000), "fat_g": num(1000),
        "fiber_g": num(300), "sugar_g": num(1000), "sodium_mg": num(50000),
        "confidence": ENUM(["high", "medium", "low"], "How sure the estimate is"),
        "notes": S("What's in it, how you estimated", maxLength=1000),
    }


FOOD_ITEM = O("One food", _item_props(), required=["name", "calories", "protein_g", "carbs_g", "fat_g"])


# --- operations ---------------------------------------------------------------------------------------------------

@dataclass
class Op:
    method: str
    path: str                      # "schedule/{id}/complete" - relative to the base URL
    op_id: str                     # operationId / MCP tool name: short, camelCase
    summary: str                   # one line
    description: str               # the details (plain words, for an AI)
    scopes: tuple = ()             # any one of them will do; () = any valid key
    display: str = ""              # the docs heading after the method, if not the path
    query: dict = field(default_factory=dict)
    body: dict = field(default_factory=dict)
    required: tuple = ()
    example: dict = None           # GET: query values; otherwise the JSON body
    returns: object = None
    status: int = 200
    public: bool = False           # no key needed (docs, specs)
    tool: bool = True              # offered as an MCP tool
    section: str = ""

    @property
    def path_params(self):
        return re.findall(r"{(\w+)}", self.path)

    @property
    def heading(self):
        return f"{self.method:<4} {self.display or re.sub(r'{(\w+)}', r'<\1>', self.path)}"

    @property
    def writes(self):
        return self.method != "GET"


@dataclass
class Section:
    key: str
    title: str
    intro: str = ""


SECTIONS = [
    Section("account", "ACCOUNT (this key, its owner, and these docs)"),
    Section("schedule", "SCHEDULE (the owner's Planner - tasks and events, one-off or repeating)", """\
Every call is a 403 if the owner can't open the Planner. Dates are YYYY-MM-DD, times HH:MM (24-hour, the
owner's local time). A repeating item is one item; each date it happens is an "occurrence".
ONE AT A TIME: a repeating TASK with no end ("one_at_a_time": true) has only one current occurrence - the
earliest not done or skipped. Completing it makes the next one appear: on schedule, the first scheduled date
after the later of its date and today (missed ones are skipped, not stacked up); with
"repeat_from_completion", today plus the repeat interval. Un-completing it puts it back."""),
    Section("meals", 'MEALS (meal planning in a space - you are the "Recipe Bot")', """\
Everything here happens in a SPACE the token owner sees: their Personal space, or a circle's they've joined and opted
in to Meals (My account > Circles). By default the one they last picked in Meals (Personal at first); send "space":
<id> - in the query string for GET, or in the JSON body - for another of theirs (ids from GET meals/spaces). A space
they don't see is a 404; leaving a circle or turning Meals off for it takes effect at once.
Slots: "breakfast" | "lunch" | "dinner" | "snack" - one meal per day per slot. Aisles: "produce" | "meat" |
"dairy" | "bakery" | "pantry" | "frozen" | "household" | "other". Quantities: a number (2, 0.5) or a string
("1 1/2"), up to 100000. "added_by" is "Recipe Bot" for what you created, else the person's name."""),
    Section("notes", "NOTES (the owner's private notes - Markdown; website feature ideas go to the ideas endpoint)", """\
Every call is a 403 if the owner can't open Notes. Tags are lowercased, "#" dropped, <=20 of <=50 chars.
Links point a note at a record the owner can open: {"app": "meals", "type": "recipe", "id": 12} - kinds:
meals/recipe, resell/ledger, resell/lot, opps/opportunity, trade/strategy, trade/option_strategy - or
{"url": "<a record's page on this site>"}."""),
    Section("fitness", "FITNESS (the owner's own workouts, food diary and weigh-ins - private to them, even within a circle)",
            """\
Every call is a 403 if the owner can't open Fitness. Dates are YYYY-MM-DD (default today, never in the future).
For an AI logging what the person ate, use the food log endpoints (fitness/food-entries): estimate each item's
macros, check foods/search first for known foods, and send everything in one POST."""),
    Section("activity", "ACTIVITY (the owner's notifications - what others changed in things shared with them and in their circles)",
            """\
Never the owner's own actions. Each event is checked when you ask: something unshared, or a circle the owner left
or opted out of, disappears at once. The owner's notification settings apply (an app or circle turned Off, or
"Only things that involve me"). Kept 90 days."""),
    Section("trading", "TRADING (site admin only)"),
    Section("reselling", "RESELLING (site admin only)"),
    Section("opportunities", "OPPORTUNITIES (site admin only)"),
    Section("admin", "WEBSITE SUGGESTIONS AND IDEAS (site admin only)"),
]
SECTION_BY_KEY = {s.key: s for s in SECTIONS}

OCCURRENCE = O("", {"occurrence_date": D("Which occurrence (required for a repeating item)")})
ATTACHMENTS_NOTE = ('"attachments": [{"id", "name", "type" (MIME), "size" (bytes), "url"}] - read-only; the url opens '
                    "with the owner's signed-in session, not with this key, and there's no upload through the API.")

_OPS = [
    # ----- account / meta ------------------------------------------------------------------------------------------
    Op("GET", "docs", "getDocs", "This guide (markdown)", """\
This text, written for an AI. No key needed; send one and only the endpoints that key can call are listed.""",
       public=True, tool=False, section="account", returns="# Albright Labs API ..."),
    Op("GET", "openapi.json", "getOpenApi", "The OpenAPI 3.1 spec of every endpoint", """\
The machine-readable spec, for AIs and tools that import actions or tools. No key needed.""",
       public=True, tool=False, section="account", returns={"openapi": "3.1.0", "paths": {"/me": {}}}),
    Op("GET", "fitness/openapi.json", "getFoodOpenApi", "The OpenAPI spec of just the food log endpoints", """\
The same spec trimmed to the food log (fitness/food-entries, foods/search). No key needed.""",
       public=True, tool=False, section="account", returns={"openapi": "3.1.0", "paths": {"/fitness/food-entries": {}}}),
    Op("GET", "me", "getMe", "Who this key acts as and what it may do", """\
Call this first. Returns the owner's username, this key's name, the scopes it can use right now (a scope for an app
the owner can no longer open doesn't count), the apps with what it may do in each, and every endpoint it can call
(method, path, operationId, summary). Any valid key.""",
       section="account", returns={"username": "sam", "key_name": "My ChatGPT", "scopes": ["schedule:read", "schedule:write"],
                                   "apps": [{"app": "Planner", "access": "read and change"}],
                                   "endpoints": [{"method": "GET", "path": "schedule", "operation_id": "listSchedule",
                                                  "summary": "Planner occurrences in a date range"}],
                                   "expires_at": None}),
    Op("GET", "me/storage", "getStorage", "The owner's attachment storage use", """\
The token owner's attachment storage (files on Planner tasks and notes, counted against whoever uploaded them).
Any valid token. Returns {"used_bytes": int, "limit_bytes": int, "percent": float (one decimal; over 100 when
above a lowered limit), "remaining_bytes": int, "tier": name of their storage plan, e.g. "Standard"}.""",
       section="account", returns={"used_bytes": 1048576, "limit_bytes": 104857600, "percent": 1.0,
                                   "remaining_bytes": 103809024, "tier": "Standard"}),

    # ----- schedule --------------------------------------------------------------------------------------------------
    Op("GET", "schedule", "listSchedule", "Planner occurrences in a date range", """\
Occurrences between start and end, inclusive (default: the 7 days from today; at most 366 days), by day
and time. Includes items shared with the owner. A one-at-a-time task lists only its current occurrence
(on its date - which may be in the past if it's overdue) and the ones already done; expand=all (optional)
lists every scheduled occurrence in the range instead. Returns {"start", "end", "occurrences": [{"item_id",
"type" ("task"|"event"|"workout"|"weigh_in" - a Fitness weigh-in: private, logged with a weight), "title", "date" (the day it shows, after any move), "occurrence_date" (the day it was scheduled - use this
to complete it), "start_time", "end_time", "kind" ("task"|"event"), "checkable", "done", "repeats",
"created_by", "owner" (whose item - first name), "mine" (false for an item someone shared with the owner),
"done_by" (who checked it off - either person, for a shared item - or null), "one_at_a_time", "checklist":
[{"id", "text", "done", "done_by"}] (that occurrence's subtasks - each occurrence starts with all open),
"attachments": [{"id", "name", "type" (MIME), "size" (bytes), "url"}] (the item's files and photos -
read-only; no upload through the API), "weigh_in" (weigh_in only: {"weight", "unit", "skipped"} once done,
else null)}]}. To log a weigh-in, POST fitness/weight - it checks off that day's weigh-in task.""",
       scopes=("schedule:read",), display="schedule?start=YYYY-MM-DD&end=YYYY-MM-DD&expand=all", section="schedule",
       query={"start": D(), "end": D(), "expand": ENUM(["all"], "all = every occurrence of one-at-a-time tasks")},
       example={"start": "2026-11-16", "end": "2026-11-22"},
       returns={"start": "2026-11-16", "end": "2026-11-22", "occurrences": [
           {"item_id": 41, "type": "task", "title": "Gym", "date": "2026-11-16", "occurrence_date": "2026-11-16",
            "start_time": "06:00", "end_time": None, "kind": "task", "checkable": True, "done": False,
            "repeats": "Weekly on Mon, Wed, Fri", "mine": True, "checklist": [], "attachments": []},
           {"item_id": 57, "type": "weigh_in", "title": "Weigh-in", "date": "2026-11-16", "occurrence_date": "2026-11-16",
            "start_time": "07:00", "end_time": None, "kind": "task", "checkable": True, "done": True, "repeats": True,
            "mine": True, "checklist": [], "attachments": [], "weigh_in": {"weight": "182.0", "unit": "lb", "skipped": False}}]}),
    Op("POST", "schedule", "addScheduleItem", "Add a task or event to the Planner (one-off or repeating)", """\
Body: {"title": str (required, <=200), "date": YYYY-MM-DD (required - the first day), "start_time": HH:MM
(optional; none = all day), "end_time": HH:MM (optional), "kind": "task"|"event" (default "task"),
"checkable": bool (events only; tasks always can be checked off), "notes": Markdown str (optional, <=50000),
and optionally a repeat - EITHER the Planner's simple choices:
  "repeat": "daily"|"every_n_days"|"weekdays"|"weekly"|"every_n_weeks"|"monthly_day"|"monthly_nth"|"yearly",
  "days": ["MO","WE","FR"] (weekly / every_n_weeks; default the date's weekday), "interval": 1-52
  (every_n_days, every_n_weeks), "month_day": 1-31 (monthly_day), "nth": 1|2|3|4|-1 and "nth_weekday": "TU"
  (monthly_nth - e.g. the 2nd Tuesday)
OR "rrule": an RFC 5545 rule using FREQ (DAILY|WEEKLY|MONTHLY|YEARLY), INTERVAL, BYDAY, BYMONTHDAY,
  BYMONTH, BYSETPOS, WKST, UNTIL, COUNT - e.g. "FREQ=WEEKLY;BYDAY=MO,WE,FR".
End a repeat with "until": YYYY-MM-DD or "count": 1-1000 (or UNTIL/COUNT in the rrule) - not both.
"repeat_from_completion": bool (optional, a repeating task with no end only) - see ONE AT A TIME.
"checklist": ["item", ...] (optional, tasks only - at most 100 items of <=200 characters): its subtasks.
"share" (optional): see SHARING.
The item belongs to the owner and shows "by <your token's name>"; it's shared the way the owner shares new
items by default. Returns 201 {"id", "title", "kind", "date", "start_time", "end_time", "checkable", "notes",
"rrule", "until", "count", "repeats" (plain English), "one_at_a_time", "repeat_from_completion", "created_by",
"checklist": [{"id", "text"}], "attachments": [], "url"}.""",
       scopes=("schedule:write",), section="schedule", status=201,
       body={"title": S(maxLength=200), "date": D("The first day"), "start_time": S("HH:MM; none = all day"),
             "end_time": S("HH:MM"), "kind": ENUM(["task", "event"]), "checkable": B("Events only"),
             "notes": S("Markdown"),
             "repeat": ENUM(["daily", "every_n_days", "weekdays", "weekly", "every_n_weeks", "monthly_day",
                             "monthly_nth", "yearly"]),
             "days": A(ENUM(["MO", "TU", "WE", "TH", "FR", "SA", "SU"])), "interval": I(minimum=1, maximum=52),
             "month_day": I(minimum=1, maximum=31), "nth": I("1-4, or -1 for the last"),
             "nth_weekday": ENUM(["MO", "TU", "WE", "TH", "FR", "SA", "SU"]), "rrule": S("RFC 5545 rule"),
             "until": D(), "count": I(minimum=1, maximum=1000), "repeat_from_completion": B(),
             "checklist": A(S(maxLength=200), maxItems=100), "share": SHARE},
       required=("title", "date"),
       example={"title": "Gym", "date": "2026-11-16", "start_time": "06:00", "repeat": "weekly", "days": ["MO", "WE", "FR"]},
       returns={"id": 41, "title": "Gym", "kind": "task", "date": "2026-11-16", "start_time": "06:00",
                "repeats": "Weekly on Mon, Wed, Fri", "created_by": "My ChatGPT", "url": "/planner/items/41/"}),
    Op("POST", "schedule/{id}/complete", "completeScheduleItem", "Check off one occurrence of a Planner item", """\
Body: {"occurrence_date": YYYY-MM-DD} (required for a repeating item; a one-off item's own date otherwise).
Checks off THAT occurrence only. Only items the owner may edit and that can be checked off (anything else is a 404
or 400). For a one-at-a-time task, completing its current occurrence makes the next one appear (see ONE AT A TIME).
Returns {"item_id", "occurrence_date", "done"}.""",
       scopes=("schedule:write",), section="schedule", body=OCCURRENCE["properties"],
       example={"occurrence_date": "2026-11-16"}, returns={"item_id": 41, "occurrence_date": "2026-11-16", "done": True}),
    Op("POST", "schedule/{id}/uncomplete", "uncompleteScheduleItem", "Un-check one occurrence of a Planner item", """\
Body: {"occurrence_date": YYYY-MM-DD} (required for a repeating item). Un-checks THAT occurrence only; for a
one-at-a-time task it puts it back. Returns {"item_id", "occurrence_date", "done"}.""",
       scopes=("schedule:write",), section="schedule", body=OCCURRENCE["properties"],
       example={"occurrence_date": "2026-11-16"}, returns={"item_id": 41, "occurrence_date": "2026-11-16", "done": False}),
    Op("POST", "schedule/{id}/checklist/{item_id}/complete", "completeChecklistItem",
       "Tick one subtask of a Planner task occurrence", """\
Body: {"occurrence_date": YYYY-MM-DD} (required for a repeating item). Ticks one subtask ("checklist" ids from GET
schedule) on THAT occurrence only. When every subtask of the occurrence is ticked the task checks itself off, if the
owner has "Complete task when all items are checked" on. A grocery-list subtask checks the line on the Meals grocery
list (the owner needs Meals). Returns {"item_id", "checklist_item_id", "occurrence_date", "done", "progress" ("2/5"),
"task_done"}.""",
       scopes=("schedule:write",), section="schedule", body=OCCURRENCE["properties"],
       example={"occurrence_date": "2026-11-16"},
       returns={"item_id": 41, "checklist_item_id": 7, "occurrence_date": "2026-11-16", "done": True,
                "progress": "2/5", "task_done": False}),
    Op("POST", "schedule/{id}/checklist/{item_id}/uncomplete", "uncompleteChecklistItem",
       "Untick one subtask of a Planner task occurrence", """\
Body: {"occurrence_date": YYYY-MM-DD} (required for a repeating item). Unticks one subtask on THAT occurrence only
(and reopens the task if it had checked itself off). Returns as completeChecklistItem.""",
       scopes=("schedule:write",), section="schedule", body=OCCURRENCE["properties"],
       example={"occurrence_date": "2026-11-16"},
       returns={"item_id": 41, "checklist_item_id": 7, "occurrence_date": "2026-11-16", "done": False,
                "progress": "1/5", "task_done": False}),

    # ----- meals -----------------------------------------------------------------------------------------------------
    Op("GET", "meals/spaces", "listMealSpaces", "The owner's Meals spaces", """\
The owner's Meals spaces. Returns {"spaces": [{"id", "name" ("Personal" or the circle's name), "personal": bool,
"current": bool (used when "space" isn't sent)}]}. Example: GET meals/plan?space=12""",
       scopes=("meals:read",), section="meals",
       returns={"spaces": [{"id": 12, "name": "Personal", "personal": True, "current": True}]}),
    Op("GET", "meals/plan", "getMealPlan", "The meal plan between two dates", """\
The plan between two dates (default: this week, Monday to Sunday; at most 62 days). Returns
{"start", "end", "entries": [{"id", "date", "slot", "title", "recipe_id" (int | null), "status"
("planned"|"cooked"|"skipped"), "notes", "added_by"}]}.""",
       scopes=("meals:read",), display="meals/plan?start=YYYY-MM-DD&end=YYYY-MM-DD", section="meals",
       query={"start": D(), "end": D(), "space": SPACE}, example={"start": "2026-11-16", "end": "2026-11-22"},
       returns={"start": "2026-11-16", "end": "2026-11-22", "entries": [
           {"id": 90, "date": "2026-11-17", "slot": "dinner", "title": "Turkey chili", "recipe_id": 12,
            "status": "planned", "notes": "", "added_by": "Recipe Bot"}]}),
    Op("POST", "meals/plan", "setMeal", "Set a day's meal (replaces what's planned in that slot)", """\
Set a day's slot - replaces whatever is planned there. Body: {"date": "YYYY-MM-DD" (required), "slot" (required),
"recipe_id": int (optional), "title": str (<=200; required without recipe_id), "notes": str (optional)}.
Returns 201 (new) or 200 (replaced) {"id", "created": bool, ...the entry}. 404 if recipe_id doesn't exist.""",
       scopes=("meals:write",), section="meals", status=201,
       body={"date": D(), "slot": SLOT, "recipe_id": I(), "title": S(maxLength=200), "notes": S(), "space": SPACE},
       required=("date", "slot"), example={"date": "2026-11-17", "slot": "dinner", "recipe_id": 12},
       returns={"id": 90, "created": True, "date": "2026-11-17", "slot": "dinner", "title": "Turkey chili"}),
    Op("DELETE", "meals/plan/{id}", "removeMeal", "Remove a plan entry the Recipe Bot created", """\
Remove a plan entry the Recipe Bot created, in any space the owner sees (403 for anyone else's). Returns
{"deleted": id}.""",
       scopes=("meals:write",), section="meals", returns={"deleted": 90}),
    Op("GET", "meals/groceries", "listGroceries", "The grocery list", """\
The space's grocery list. Returns {"items": [{"id", "name", "quantity" (str | null), "unit", "aisle", "note",
"checked", "added_by"}]}.""",
       scopes=("meals:read",), section="meals", query={"space": SPACE},
       returns={"items": [{"id": 5, "name": "milk", "quantity": "1", "unit": "gal", "aisle": "dairy", "note": "",
                           "checked": False, "added_by": "Sam"}]}),
    Op("POST", "meals/groceries", "addGroceries", "Add one or many grocery items", """\
Add one item {"name": str (required, <=120), "quantity", "unit" (<=20), "aisle" (optional - guessed from the
name), "note" (<=200)} or many: {"items": [ ...up to 100 of those ]}. An item merges into an UNCHECKED line
with the same name and unit (quantities added). Returns 201 {"items": [{"id", "merged": bool, ...}]}.""",
       scopes=("meals:write",), section="meals", status=201,
       body={"name": S(maxLength=120), "quantity": NUM_OR_TEXT, "unit": S(maxLength=20), "aisle": S(), "note": S(maxLength=200),
             "items": A(O("One item", {"name": S(), "quantity": NUM_OR_TEXT, "unit": S(), "aisle": S(), "note": S()}),
                        maxItems=100), "space": SPACE},
       example={"items": [{"name": "milk", "quantity": 1, "unit": "gal"}, {"name": "eggs", "quantity": 12}]},
       returns={"items": [{"id": 5, "name": "milk", "merged": False}, {"id": 6, "name": "eggs", "merged": True}]}),
    Op("PATCH", "meals/groceries/{id}", "updateGrocery", "Change or check off a grocery line", """\
Body: any of {"quantity", "note", "checked": true|false}. Any line in a space the owner sees. Returns the item.""",
       scopes=("meals:write",), section="meals", body={"quantity": NUM_OR_TEXT, "note": S(), "checked": B()},
       example={"checked": True}, returns={"id": 5, "name": "milk", "checked": True}),
    Op("GET", "meals/ideas", "listMealIdeas", "Meal ideas, newest first", """\
Meal ideas, newest first (up to 200). Returns {"ideas": [{"id", "title", "notes", "link", "status"
("new"|"planned"|"declined"), "added_by", "created_at"}]}.""",
       scopes=("meals:read",), section="meals", query={"space": SPACE},
       returns={"ideas": [{"id": 3, "title": "Sheet-pan fajitas", "status": "new", "added_by": "Recipe Bot"}]}),
    Op("POST", "meals/ideas", "addMealIdea", "Add a meal idea", """\
Body: {"title": str (required, <=200), "notes": str (optional), "link": http(s) URL (optional)}.
Returns 201 {"id", ...}.""",
       scopes=("meals:write",), section="meals", status=201,
       body={"title": S(maxLength=200), "notes": S(), "link": S("http(s) URL"), "space": SPACE}, required=("title",),
       example={"title": "Sheet-pan fajitas"}, returns={"id": 3, "title": "Sheet-pan fajitas", "status": "new"}),
    Op("GET", "meals/recipes", "listRecipes", "Recipes (with ingredients, steps and ratings)", """\
Recipes, most recently changed first (up to 200). scope: "space" (default - the space's; "household" still works),
"shared" (recipes from other spaces shared with the owner - read-only) or "library" (the shared recipe library -
read-only). q filters by title. Rating filters (the Recipes page's), judged by the stamps of the people who see the
space: rated_by = "me" (default) | "anyone" (anyone in the space) | <the user id of someone in it>; rating = 1-6
(Won't eat again, Not for me, Needs tweaks, Will eat again, Favorite, Staple) or "none" (not rated) - repeat it
for several; love=1: at least two people in the space rated it and all Favorite or Staple. Each recipe:
{"id", "title", "description", "ingredients": [{"name", "quantity", "unit", "note"}], "steps": [str],
"prep_minutes", "cook_minutes", "servings", "tags", "source_url", "image_url", "notes", "added_by", "updated_at",
"ours": bool, "space": its space id, "visibility": "household" (this space only) | "library", "ratings": [{"by",
"level", "label"}]}. Returns {"scope", "recipes"}. Ratings are read-only here.""",
       scopes=("meals:read",), display="meals/recipes?scope=space&q=<words>&rated_by=me&rating=5&love=1", section="meals",
       query={"scope": ENUM(["space", "shared", "library"]), "q": S("Words in the title"), "rated_by": S(),
              "rating": S("1-6 or none"), "love": ENUM(["1"]), "space": SPACE},
       example={"q": "chili"},
       returns={"scope": "space", "recipes": [{"id": 12, "title": "Turkey chili", "servings": 6, "ingredients": [
           {"name": "ground turkey", "quantity": "2", "unit": "lb", "note": ""}], "steps": ["Brown the turkey."]}]}),
    Op("POST", "meals/recipes", "saveRecipe", "Create a recipe, or update the one with the same title", """\
Create a recipe in the space, or update the space's one with the same title (case-insensitive) -
only the fields you send change.
Body: {"title": str (required, <=200), "description", "notes" (<=5000), "ingredients": [{"name" (required,
<=120), "quantity" (<=20), "unit" (<=20), "note" (<=200)} or "2 lb chicken thighs", ...] (<=100),
"steps": [str (<=2000)] (<=100), "prep_minutes" / "cook_minutes" (0-2880), "servings" (1-100),
"tags": [str (<=50)] (<=20), "source_url" / "image_url" (http(s), <=500), "share" (optional - see SHARING)}.
Returns 201 (created) or 200 (updated) {"id": int, "created": bool}.""",
       scopes=("meals:write",), section="meals", status=201,
       body={"title": S(maxLength=200), "description": S(), "notes": S(), "ingredients": A({"type": ["object", "string"]}),
             "steps": A(S()), "prep_minutes": I(), "cook_minutes": I(), "servings": I(), "tags": A(S()),
             "source_url": S(), "image_url": S(), "share": SHARE, "space": SPACE},
       required=("title",),
       example={"title": "Turkey chili", "servings": 6, "ingredients": ["2 lb ground turkey", "1 can black beans"],
                "steps": ["Brown the turkey.", "Simmer 30 minutes."]},
       returns={"id": 12, "created": True}),
    Op("POST", "meals/recipes/import", "importRecipe", "Import a recipe from a web page", """\
Import a recipe from a link. Only that one page is read (never anything it links to): its recipe data
(schema.org JSON-LD or microdata), or - if it has none - the AI reads the page's visible text, keeping only
what is on the page; those recipes are marked "needs review". A link that's already saved (tracking
parameters like utm_* and #fragments ignored) returns the saved recipe instead of a copy.
Body: {"url": http(s) URL (required, <=2000), "add_to_plan": {"date": "YYYY-MM-DD", "slot":
"breakfast"|"lunch"|"dinner"|"snack"} (optional), "add_to_groceries": bool (optional - adds the ingredients
not already on the list), "share" (optional - see SHARING)}.
Returns 201 (new) or 200 (already saved) {"id": int, "title": str, "status": "created"|"already_saved"|
"needs_review", "url": the recipe's page, "plan": {...} (with add_to_plan), "groceries": {"added", "merged",
"already_listed"} (with add_to_groceries)}.
Errors ({"error": "..."}): 400 not a public http(s) page; 422 "No recipe found on that page." (or the page is
over 3 MB); 502 the page couldn't be loaded; 504 it took over 10 seconds; 503 no recipe data and the AI
reader is unavailable or over its daily limit.""",
       scopes=("meals:write",), section="meals", status=201,
       body={"url": S("http(s) URL", maxLength=2000), "add_to_plan": O("", {"date": D(), "slot": SLOT}),
             "add_to_groceries": B(), "share": SHARE, "space": SPACE},
       required=("url",),
       example={"url": "https://www.example.com/recipes/chili", "add_to_plan": {"date": "2026-11-17", "slot": "dinner"},
                "add_to_groceries": True},
       returns={"id": 13, "title": "Chili", "status": "created", "url": "/meals/recipes/13/",
                "plan": {"date": "2026-11-17", "slot": "dinner"}, "groceries": {"added": 8, "merged": 1, "already_listed": 2}}),

    # ----- notes -----------------------------------------------------------------------------------------------------
    Op("GET", "notes", "listNotes", "The owner's own notes (search by words or tag)", """\
The owner's own notes (never notes others shared with them), pinned first then most recently changed, or
best matches first with q (full-text over title, tags and body; each word also matches as a prefix).
Up to 200. Returns {"notes": [{"id", "title", "tags", "pinned", "archived", "created_by", "created_at",
"updated_at", "attachments"}]}. """ + ATTACHMENTS_NOTE,
       scopes=("notes:read",), display="notes?q=<words>&tag=<tag>&archived=1", section="notes",
       query={"q": S("Words to search for"), "tag": S(), "archived": ENUM(["1"], "1 = archived notes")},
       example={"q": "gift"},
       returns={"notes": [{"id": 8, "title": "Gift ideas", "tags": ["family"], "pinned": False, "archived": False,
                           "created_by": "Sam", "attachments": []}]}),
    Op("POST", "notes", "addNote", "Save a new note for the owner", """\
Body: {"title": str (required, <=200), "body": Markdown str (optional, <=100000), "tags": [str] (optional),
"pinned": bool (optional), "link": {"app", "type", "id"} or {"url"} (optional), "share" (optional - see SHARING)}.
The note belongs to the owner and shows "by <your token's name>". Returns 201 {"id", ..., "url", "linked"}.""",
       scopes=("notes:write",), section="notes", status=201,
       body={"title": S(maxLength=200), "body": S("Markdown"), "tags": A(S()), "pinned": B(),
             "link": O('{"app", "type", "id"} or {"url"}'), "share": SHARE},
       required=("title",), example={"title": "Gift ideas", "body": "- Board game\n- Scarf", "tags": ["family"]},
       returns={"id": 8, "title": "Gift ideas", "url": "/notes/8/", "linked": None}),
    Op("PATCH", "notes/{id}", "updateNote", "Add to or replace a note's text, or add tags", """\
Only notes this token created, or that are shared with the owner to edit (anything else is a 404).
Body: {"append": str} (added after a blank line) or {"body": str} (replaces it), and/or {"tags": [str]}
(added to the note's tags). Returns the note.""",
       scopes=("notes:write",), section="notes", body={"append": S(), "body": S(), "tags": A(S())},
       example={"append": "- Candle"}, returns={"id": 8, "title": "Gift ideas", "tags": ["family"]}),

    # ----- fitness ---------------------------------------------------------------------------------------------------
    Op("POST", "fitness/food-entries", "logFood", "Log what the owner ate: 1-20 items with estimated macros", """\
An AI's estimate of what the owner ate - 1 to 20 items, all saved or none: {"date": "YYYY-MM-DD" (optional:
the owner's today in their time zone, up to 60 days back), "meal": "breakfast"|"lunch"|"dinner"|"snack"
(optional: by the owner's local time - before 10:30 breakfast, before 15:00 lunch, before 21:00 dinner, else
snack), "items": [{"name" (required), "quantity", "unit" (free text, e.g. "bowl"), "grams" (<= 5000),
"calories" (required, <= 5000), "protein_g", "carbs_g", "fat_g" (required), "fiber_g", "sugar_g", "sodium_mg",
"confidence": "high"|"medium"|"low", "notes"}], "source_text": the person's words, "idempotency_key": any
string}. No negatives. A 400 lists {"errors": [{"field": "items[0].calories", "message"}]} and saves nothing.
Returns 201 {"date", "entries": [{"id", "meal", "name", ..., "source": "ai", "confidence", "edited"}],
"totals": {"calories", "protein_g", "carbs_g", "fat_g"} for the day, "targets": the same or null, "warnings":
[calories that differ from 4*protein + 4*carbs + 9*fat by more than 25% and 50 kcal - saved anyway]}. The
same idempotency_key within 24 hours returns those entries again (200, "replayed": true). At most 60 food log
changes an hour per key.""",
       scopes=("fitness:food:write",), section="fitness", status=201,
       body={"date": D("Default: the owner's today"), "meal": {**SLOT, "description": "Default: by the owner's local time"},
             "items": A(FOOD_ITEM, minItems=1, maxItems=20), "source_text": S("What the person said, word for word", maxLength=4000),
             "idempotency_key": S("A new random string per meal; the same one within 24 hours returns the first result",
                                  maxLength=100)},
       required=("items",),
       example={"meal": "lunch", "items": [{"name": "Chicken burrito bowl", "quantity": 1, "unit": "bowl", "calories": 720,
                                            "protein_g": 48, "carbs_g": 70, "fat_g": 24, "confidence": "medium",
                                            "notes": "white rice, black beans, mild salsa, cheese"}],
                "source_text": "had a chipotle chicken bowl for lunch", "idempotency_key": "lunch-2026-11-16-a1b2"},
       returns={"date": "2026-11-16", "entries": [{"id": 301, "meal": "lunch", "name": "Chicken burrito bowl",
                                                   "calories": 720, "protein_g": 48, "carbs_g": 70, "fat_g": 24,
                                                   "source": "ai", "confidence": "medium", "edited": False}],
                "totals": {"calories": 1450, "protein_g": 95, "carbs_g": 150, "fat_g": 50},
                "targets": {"calories": 2200, "protein_g": 150, "carbs_g": 220, "fat_g": 70}, "warnings": []}),
    Op("GET", "fitness/food-entries", "getFoodDay", "A day's food entries, totals and targets", """\
A day's entries (default the owner's today, up to a year back), "totals" and "targets", as logFood returns them.""",
       scopes=("fitness:food:read",), display="fitness/food-entries?date=YYYY-MM-DD", section="fitness",
       query={"date": D("Default: the owner's today")}, example={"date": "2026-11-16"},
       returns={"date": "2026-11-16", "entries": [], "totals": {"calories": 0, "protein_g": 0, "carbs_g": 0, "fat_g": 0},
                "targets": None, "warnings": []}),
    Op("PATCH", "fitness/food-entries/{id}", "fixFoodEntry", "Fix any field of one food entry", """\
Fix any field of one of the owner's entries (the item fields, "meal", "date"). Returns {"entry", "totals",
"targets", "warnings"}. Someone else's id is a 404.""",
       scopes=("fitness:food:write",), section="fitness", body={**_item_props(), "meal": SLOT, "date": D()},
       example={"calories": 650, "notes": "no cheese"},
       returns={"entry": {"id": 301, "calories": 650}, "totals": {"calories": 1380}, "targets": None, "warnings": []}),
    Op("DELETE", "fitness/food-entries/{id}", "deleteFoodEntry", "Remove one food entry", """\
Remove one of the owner's entries. Returns {"deleted": id, "date", "totals", "targets"}.""",
       scopes=("fitness:food:write",), section="fitness",
       returns={"deleted": 301, "date": "2026-11-16", "totals": {"calories": 730}, "targets": None}),
    Op("GET", "fitness/food-entries/recent", "recentFoods", "The owner's most-logged foods, ready to log again", """\
The owner's most-logged foods over 60 days (1-50): {"foods": [{"times_logged", "last_logged", "meal",
"source", "item": a ready item for logFood}]} - for "the usual breakfast".""",
       scopes=("fitness:food:read",), display="fitness/food-entries/recent?limit=20", section="fitness",
       query={"limit": I(minimum=1, maximum=50, default=20)}, example={"limit": 5},
       returns={"foods": [{"times_logged": 9, "last_logged": "2026-11-15", "meal": "breakfast", "source": "ai",
                           "item": {"name": "Greek yogurt with berries", "calories": 220, "protein_g": 18,
                                    "carbs_g": 25, "fat_g": 5}}]}),
    Op("GET", "fitness/foods/search", "searchFoods", "Search USDA foods, the owner's foods and recipes", """\
The site's one search box: {"q", "groups": [{"group": "recent"|"mine"|"recipes"|"usda", "title", "items":
[{"kind", "label", "detail", "fdc_id" | "my_food" | "recipe_id", "units": [{"unit", "label"}], "log": a ready
POST fitness/food body for 1 of the first unit, "per_serving": {"serving", "grams", "calories", "protein_g",
"carbs_g", "fat_g", "fiber_g"} for one of its first unit, or null}]}], "note" (why all-foods search is missing, or
null)}. A USDA food's units are its household portions plus "g" and "oz" (always), and ml / fl oz / cup / tbsp /
tsp when known.""",
       scopes=("fitness:read", "fitness:food:read"), display="fitness/foods/search?q=greek yogurt", section="fitness",
       query={"q": S("What to look for", maxLength=100)}, required=("q",), example={"q": "greek yogurt"},
       returns={"q": "greek yogurt", "groups": [{"group": "usda", "title": "All foods", "items": [
           {"kind": "usda", "label": "Yogurt, Greek, plain, nonfat", "fdc_id": 2259793,
            "per_serving": {"serving": "1 container (170 g)", "grams": 170, "calories": 100, "protein_g": 17,
                            "carbs_g": 6, "fat_g": 0.7, "fiber_g": 0}}]}], "note": None}),
    Op("POST", "fitness/food", "logFoodByName", "Log one diary entry from a known food, recipe or quick macros", """\
One diary entry, by ONE of: {"fdc_id": 2259793, "quantity": "1/4", "unit": "p:1 container (170 g)"} (a USDA
food picked from foods/search - "quantity" is a number or a fraction like "1/4" or "1 1/2"; "unit" one of its
units, by "unit" or "label"; "g" and "oz" always work); {"my_food": "Protein shake", "quantity": 0.5, "unit":
"serving" or "g"} (any of that food's units); {"recipe_id": 12} or {"recipe": "Turkey chili"} (a Meals recipe the
owner can see, with nutrition) x "servings"; or quick macros {"label": "Latte", "calories": 190, "protein": 10,
"carbs": 18, "fat": 7}. Optional "slot": "breakfast"|"lunch"|"dinner"|"snack" (default snack), "date", "servings"
(default 1, up to 50). Names match exactly (any case). NEVER GUESSED: an unmatched name logs nothing and returns 422
{"error", "suggestions": {"recipes": [{"id", "title"}], "my_foods": [names], "usda": [{"fdc_id", "name"}]}}. An
unknown unit logs nothing: 422 {"error", "units": [{"unit", "label"}]}. Returns 201 {"id", "date", "slot", "label",
"source", "servings", "quantity", "unit", "amount", "grams", "fdc_id", "calories", "protein", "carbs", "fat",
"fiber"}. To log an AI's estimates, use logFood instead.""",
       scopes=("fitness:write",), section="fitness", status=201,
       body={"fdc_id": I(), "my_food": S(), "recipe_id": I(), "recipe": S(), "quantity": NUM_OR_TEXT, "unit": S(),
             "servings": N(maximum=50), "label": S(), "calories": N(), "protein": N(), "carbs": N(), "fat": N(),
             "slot": SLOT, "date": D()},
       example={"recipe": "Turkey chili", "servings": 1.5, "slot": "dinner"},
       returns={"id": 302, "date": "2026-11-16", "slot": "dinner", "label": "Turkey chili", "source": "recipe",
                "servings": 1.5, "calories": 540, "protein": 45, "carbs": 40, "fat": 18, "fiber": 10}),
    Op("GET", "fitness/food/entries", "getFoodDiary", "One day's food diary (older format)", """\
One day's diary (default today, up to a year back): {"date", "entries": [as logFoodByName returns], by
meal, "totals": {"calories", "protein", "carbs", "fat", "fiber"}}. getFoodDay gives the same day with targets.""",
       scopes=("fitness:read",), display="fitness/food/entries?date=YYYY-MM-DD", section="fitness",
       query={"date": D()}, example={"date": "2026-11-16"},
       returns={"date": "2026-11-16", "entries": [], "totals": {"calories": 0, "protein": 0, "carbs": 0, "fat": 0, "fiber": 0}}),
    Op("POST", "fitness/workouts", "logWorkout", "Log a finished workout for today", """\
Logs a finished workout for today. EITHER {"text": "squat 225x5x3, bench 185x8,8,7, run 3.1mi 28:30"} (the
site's quick log: weight x reps x sets, or reps per set "8,8,7"; cardio a distance and/or a time; "kg" after a
weight for kilograms) OR {"exercises": [{"exercise": "Bench press", "sets": [{"reps": 8, "weight": 185,
"unit": "lb"}]}, {"exercise": "Running", "sets": [{"distance": 3.1, "distance_unit": "mi", "duration_seconds":
1710}]}]}; optional "name" and "share" (see SHARING). Exercise names match the owner's library (aliases like
"bench", "squat", "ohp"). When it matches today's planned program day it counts as that workout and checks off its
Planner task. Returns 201 {"id", "date", "name", "exercises": [{"exercise", "sets"}], "unknown": [{"text",
"problem", "suggestions": [exercise names]}] (NOT logged - confirm the name and send again), "records": [{"exercise",
"kind", "text"}] (new personal records), "program_day", "planner_completed", "url"}. 422 (with "unknown") if nothing
could be logged.""",
       scopes=("fitness:write",), section="fitness", status=201,
       body={"text": S("Quick log text"), "exercises": A(O("", {"exercise": S(), "sets": A(O())})), "name": S(),
             "share": SHARE},
       example={"text": "squat 225x5x3, bench 185x8,8,7"},
       returns={"id": 77, "date": "2026-11-16", "name": "Workout", "exercises": [{"exercise": "Back squat", "sets": 3}],
                "unknown": [], "records": [], "program_day": None, "planner_completed": False, "url": "/fitness/workouts/77/"}),
    Op("POST", "fitness/weight", "logWeight", "Log today's weigh-in", """\
Body: {"weight": 182.4 (required), "unit": "lb"|"kg" (default the owner's), "date", "note"}. One weigh-in a
day - a second one that day replaces it (200 instead of 201). Also checks off that day's Planner weigh-in task
(type "weigh_in") when the owner has one. Returns {"id", "date", "weight", "unit", "replaced", "planner_completed"}.""",
       scopes=("fitness:write",), section="fitness", status=201,
       body={"weight": N(), "unit": ENUM(["lb", "kg"]), "date": D(), "note": S()}, required=("weight",),
       example={"weight": 182.4, "unit": "lb"},
       returns={"id": 19, "date": "2026-11-16", "weight": 182.4, "unit": "lb", "replaced": False, "planner_completed": True}),
    Op("GET", "fitness/today", "getFitnessToday", "Today's planned workout, workouts done and macros vs targets", """\
Returns {"date", "planned": [{"name", "program", "done"}], "workouts_done": [{"id", "name"}], "macros":
{"calories"|"protein"|"carbs"|"fat"|"fiber": {"eaten", "target", "remaining" (negative = over), "tracked"}}}.""",
       scopes=("fitness:read",), section="fitness",
       returns={"date": "2026-11-16", "planned": [{"name": "Upper", "program": "Strength", "done": False}],
                "workouts_done": [], "macros": {"calories": {"eaten": 1450, "target": 2200, "remaining": 750, "tracked": True}}}),
    Op("GET", "fitness/summary", "getFitnessSummary", "Weekly fitness summary (workouts, volume, food)", """\
The last 1-12 weeks (default 4), this week first: {"weeks": [{"week_of" (Monday), "workouts_done",
"workouts_planned", "volume_lb", "volume_change_pct" (vs the week before, or null), "new_records": [{"exercise",
"text"}], "days_food_logged", "avg_calories", "avg_protein" (over days with food logged), "target_calories",
"target_protein"}]}.""",
       scopes=("fitness:read",), display="fitness/summary?weeks=4", section="fitness",
       query={"weeks": I(minimum=1, maximum=12, default=4)}, example={"weeks": 2},
       returns={"weeks": [{"week_of": "2026-11-16", "workouts_done": 2, "workouts_planned": 3, "volume_lb": 21500,
                           "volume_change_pct": 4.5, "new_records": [], "days_food_logged": 1, "avg_calories": 1450}]}),
    Op("GET", "fitness/programs", "listPrograms", "The owner's workout programs", """\
The owner's programs: {"programs": [{"id", "name", "status": "active"|"paused"|"draft", "archived", "start_date",
"weeks" (null = no end), "planner" (wants it on the Planner), "on_planner_now", "time" ("HH:MM" or null = all day),
"days": [{"id", "name", "weekdays" (0 = Monday), "start_url", "exercises": [{"exercise", "target", "sets",
"reps", "minutes", "miles"}]}], "url"}]}.""",
       scopes=("fitness:read",), section="fitness",
       returns={"programs": [{"id": 4, "name": "Strength", "status": "active", "days": [
           {"id": 9, "name": "Upper", "weekdays": [0, 3], "exercises": [{"exercise": "Barbell Bench Press", "sets": 4, "reps": 6}]}]}]}),
    Op("POST", "fitness/programs", "createProgram", "Create and turn on a workout program", """\
Creates and turns on a program in one call: {"name", "days": [{"name": "Upper", "weekdays": [0, 3] or ["mon",
"thu"], "exercises": [{"exercise": "Barbell Bench Press", "sets": 4, "reps": 6} or {"exercise": "Running",
"minutes": 30} or {"miles": 3}]}], "start_date" (default today), "weeks" (1-104, default no end), "planner":
true|false (default false), "time": "06:30" (optional, with planner)}. Exercise names must match the owner's
library exactly (any case) - NEVER GUESSED: any unknown name creates nothing and returns 422 {"error",
"unknown": [{"exercise", "suggestions"}]}. A target left out gets the site's default (3 x 10, cardio 20 min).
Returns 201 with the program (as in listPrograms) plus "planner_tasks" (how many went on the Planner).""",
       scopes=("fitness:write",), section="fitness", status=201,
       body={"name": S(), "days": A(O("", {"name": S(), "weekdays": A({"type": ["integer", "string"]}),
                                          "exercises": A(O())})),
             "start_date": D(), "weeks": I(minimum=1, maximum=104), "planner": B(), "time": S("HH:MM")},
       required=("name", "days"),
       example={"name": "Strength", "days": [{"name": "Upper", "weekdays": ["mon", "thu"],
                                              "exercises": [{"exercise": "Barbell Bench Press", "sets": 4, "reps": 6}]}]},
       returns={"id": 4, "name": "Strength", "status": "active", "planner_tasks": 0}),
    Op("POST", "fitness/programs/{id}/pause", "pauseProgram", "Pause a workout program", """\
Pause: off Today and the Planner after today (history stays; the Planner choice is kept). Returns the program. 404 if
it isn't the owner's; 409 for an unfinished draft.""",
       scopes=("fitness:write",), section="fitness", returns={"id": 4, "name": "Strength", "status": "paused"}),
    Op("POST", "fitness/programs/{id}/resume", "resumeProgram", "Resume a paused workout program", """\
Resume: back on (an archived one is restored too), and back on the Planner when it was. Returns the program. 404 if it
isn't the owner's; 409 for an unfinished draft.""",
       scopes=("fitness:write",), section="fitness", returns={"id": 4, "name": "Strength", "status": "active"}),
    Op("DELETE", "fitness/programs/{id}", "deleteProgram", "Delete a workout program (logged workouts stay)", """\
Deletes one of the owner's programs (404 if it isn't theirs). Logged workouts are never deleted - they keep the
program's and day's names ("Upper A (deleted program)"). remove_scheduled (JSON body or query string, true/false,
default true): true ends its upcoming Planner tasks (past and checked-off ones stay); false keeps them as ordinary
tasks no longer linked to the program. Returns {"deleted": id, "name", "workouts_kept", "planner_ended",
"planner_kept"}.""",
       scopes=("fitness:write",), section="fitness", query={"remove_scheduled": B(default=True)},
       returns={"deleted": 4, "name": "Strength", "workouts_kept": 12, "planner_ended": 6, "planner_kept": 0}),

    # ----- activity --------------------------------------------------------------------------------------------------
    Op("GET", "activity", "listActivity", "The owner's notifications (what others changed)", """\
Newest first, changed after `since` (default: the last 24 hours), up to `limit` (1-200, default 50).
Returns {"since", "events": [{"id", "app", "verb", "text" ("Kate added 5 items to the grocery list"),
"actor", "via" (a bot's name, or null), "summary", "count" (merged repeats), "circle": {"id", "name"} or null,
"url" (a path on the site), "created_at", "updated_at", "unread"}]}.""",
       scopes=("activity:read",), display="activity?since=<ISO date or datetime>&limit=50", section="activity",
       query={"since": S("ISO date or date-time"), "limit": I(minimum=1, maximum=200, default=50)},
       example={"since": "2026-11-15"},
       returns={"since": "2026-11-15T00:00:00Z", "events": [{"id": 55, "app": "meals", "verb": "added",
                                                             "text": "Kate added 5 items to the grocery list",
                                                             "actor": "Kate", "via": None, "unread": True}]}),

    # ----- admin-only: trading / reselling / opportunities / ideas ---------------------------------------------------
    Op("POST", "trading/notes", "addTradingNote", "Post a note to the Grok Bot page (Trading)", """\
Body: {"date": "YYYY-MM-DD" (optional, default today), "title": str (required, <=200), "text": str (required),
"strategy": str (optional - one of the owner's strategies, by name or by one of its prefixes, any capitalization)}
Shown on the owner's Grok Bot page under "Bot notes", newest first, and on that strategy's page when tagged.
Returns 201 {"id": int, "strategy": name or null}. An unknown strategy is a 400 listing the valid ones.""",
       scopes=("trading:notes",), section="trading", status=201,
       body={"date": D(), "title": S(maxLength=200), "text": S(), "strategy": S()}, required=("title", "text"),
       example={"title": "Closed SPY spread", "text": "Took profit at 50%.", "strategy": "eng"},
       returns={"id": 3, "strategy": "Engine"}),
    Op("GET", "trading/strategies", "listStrategies", "The owner's bot strategies", """\
The owner's bot strategies, so a bot can confirm its prefix: {"strategies": [{"name": str, "prefixes": [str],
"status": "active"|"paused"|"retired"}]}, in the owner's display order. An order counts toward a strategy when
its client_order_id starts with one of its prefixes (any capitalization; the longest matching prefix wins).""",
       scopes=("trading:notes",), section="trading",
       returns={"strategies": [{"name": "Engine", "prefixes": ["eng"], "status": "active"}]}),
    Op("GET", "reseller/lots", "listLots", "Today's auction lots worth a look", """\
Today's live auction lots worth a look: scanner candidates and "Check by hand" lots (low-confidence parses that
still fit under the max bid). Returns {"lots": [...]}, soonest-ending first. Each lot:
id, list ("candidate" | "check_by_hand"), title, url, source, category, current_bid, max_bid (the scanner's
computed max), headroom (max_bid - current_bid), ends_at, image_url, confidence ("high"|"medium"|"low"),
flags (scanner notes, e.g. "weight_mismatch", "key_date_verify"), matched_keyword.""",
       scopes=("reseller:read",), section="reselling",
       returns={"lots": [{"id": 1, "list": "candidate", "title": "Lot of 10 games", "current_bid": "12.00",
                          "max_bid": "30.00", "headroom": "18.00", "confidence": "high"}]}),
    Op("POST", "reseller/lots/{id}/notes", "addLotNote", "Leave a second-opinion note on a lot", """\
Body: {"text": str}. A second-opinion note shown in that lot's expandable row, labeled "Grok". Returns 201 {"id"}.""",
       scopes=("reseller:suggest",), section="reselling", status=201, body={"text": S()}, required=("text",),
       example={"text": "Box looks water-damaged in photo 3."}, returns={"id": 5}),
    Op("GET", "reseller/inventory", "listInventory", "The owner's unsold ledger items", """\
The owner's unsold ledger items. Returns {"items": [...]}. Each item:
id, item, ledger_type ("resale"|"personal"), status, cost, list_price, platform, listed_at, days_listed
(null if not listed), next_markdown {label, step_days, due (bool), due_date} or null.""",
       scopes=("reseller:read",), section="reselling",
       returns={"items": [{"id": 2, "item": "Vintage camera", "status": "listed", "cost": "20.00", "list_price": "75.00",
                           "days_listed": 12, "next_markdown": None}]}),
    Op("POST", "reseller/inventory/{id}/suggestions", "suggestPrice", "Suggest a price or listing for an item", """\
Body: {"suggested_price": "12.50" (optional), "listing_draft": str (optional), "reason": str (optional)} -
at least one of suggested_price / listing_draft. Saved as PENDING on the item's page; the owner accepts or
dismisses it. Returns 201 {"id": int, "status": "pending"}.""",
       scopes=("reseller:suggest",), section="reselling", status=201,
       body={"suggested_price": S("Decimal dollars"), "listing_draft": S(), "reason": S()},
       example={"suggested_price": "65.00", "reason": "Similar sold listings average $64."},
       returns={"id": 7, "status": "pending"}),
    Op("GET", "opportunities/new", "listNewOpportunities", "Opportunities found in the last 7 days", """\
Opportunities found in the last 7 days that aren't blocked by the safety check or excluded (spending above
budget). Jobs from job boards whose terms forbid passing listings on (Remotive, Remote OK, Himalayas, Jobicy,
Arbeitnow) are never included. Returns {"opportunities": [...]}. Each:
id, type ("crypto"|"income"), title, url, discovered_at, reward_usd, reward_type, pay_type, pay_rate_usd,
hours_estimate, ev_mid, ev_per_hour, confidence, risk_score, safety_verdict ("ok"|"warn"), safety_reasons,
deadline, application_deadline, skills_required, skill_match, location_requirement, status,
text (the posting text, up to 3000 characters).""",
       scopes=("opportunities:read",), section="opportunities",
       returns={"opportunities": [{"id": 9, "type": "income", "title": "Django contractor", "safety_verdict": "ok"}]}),
    Op("POST", "opportunities", "addOpportunity", "Add a job or freelance opening to the Opps inbox", """\
Add a job or freelance opening to the owner's Opps inbox (only they see it; it's checked for safety and skill
fit, never sent to the AI). Body: {"title" (required), "url" (required, http(s)), "company", "work_type",
"pay_type" ("hourly"|"fixed"|"prize"|"bonus"), "pay_rate_usd", "location_requirement",
"skills_required": [str], "posted_at", "application_deadline" (ISO date-times), "text" (the posting)}
-> 201 {"id", "created": true, "safety_verdict"}; the same url again -> 200 {"id", "created": false}.""",
       scopes=("opportunities:write",), section="opportunities", status=201,
       body={"title": S(), "url": S(), "company": S(), "work_type": S(),
             "pay_type": ENUM(["hourly", "fixed", "prize", "bonus"]), "pay_rate_usd": N(), "location_requirement": S(),
             "skills_required": A(S()), "posted_at": S(), "application_deadline": S(), "text": S()},
       required=("title", "url"), example={"title": "Django contractor", "url": "https://example.com/jobs/1"},
       returns={"id": 9, "created": True, "safety_verdict": "ok"}),
    Op("POST", "opportunities/{id}/notes", "addOpportunityNote", "Leave a note on an opportunity", """\
Body: {"text": str}. Shown on the opportunity's page labeled "Grok". Returns 201 {"id": int}.""",
       scopes=("opportunities:suggest",), section="opportunities", status=201, body={"text": S()}, required=("text",),
       example={"text": "Deadline is Friday."}, returns={"id": 4}),
    Op("POST", "opportunities/{id}/proposal-draft", "draftProposal", "Leave a proposal draft on an opportunity", """\
Body: {"text": str}. A proposal draft shown on the opportunity's page labeled "Grok". It is never sent
anywhere - the owner decides. Returns 201 {"id": int}.""",
       scopes=("opportunities:suggest",), section="opportunities", status=201, body={"text": S()}, required=("text",),
       example={"text": "Hi - I've built three Django apps like this..."}, returns={"id": 5}),
    Op("POST", "site-suggestions", "suggestSiteChange", "Send a website improvement idea to the Admin inbox", """\
A website improvement idea for the owner's Admin inbox.
Body: {"app": "trade" | "resell" | "opps" | "all" (required), "area": str (optional, <=100, e.g. "Watchlist page"),
       "title": str (required, <=200), "description": str (required - what to change and why),
       "priority": "low" | "medium" | "high" (optional, default "medium")}
It lands in the owner's inbox as new; the owner either declines it or promotes it to one of their Ideas
(status promoted). Nothing changes on the site by itself. Returns 201 {"id": int, "status": "new"}.""",
       scopes=("site:suggest",), section="admin", status=201,
       body={"app": ENUM(["trade", "resell", "opps", "all"]), "area": S(maxLength=100), "title": S(maxLength=200),
             "description": S(), "priority": ENUM(["low", "medium", "high"])},
       required=("app", "title", "description"),
       example={"app": "trade", "title": "Show time left in red", "description": "Under an hour left is easy to miss."},
       returns={"id": 14, "status": "new"}),
    Op("GET", "site-suggestions", "listSiteSuggestions", "Bot website suggestions and where they stand", """\
Bot suggestions and where they stand. With suggestions:read: every bot suggestion. With only site:suggest:
just the ones THIS token sent. Without either: 403 {"error": "missing scope", "required": "suggestions:read"}.
Query (all optional): status=new,promoted (one or more of new, declined, promoted); app=trade,opps (one or more of
trade, resell, opps, all); priority=high (one or more of low, medium, high); created_since / updated_since
(YYYY-MM-DD or an ISO date-time); q (words in the title); include=description (add each suggestion's description);
sort=created_at (default) | status_changed_at - newest first either way; page=N, page_size=1-100 (default 25).
Returns {"suggestions": [{"id", "title", "app", "priority", "status", "source" (the name of the bot token that
sent it, or null), "created_at", "status_changed_at", "description" (only with include=description)}],
"page", "page_size", "pages", "total", "next_page" (null on the last page), "own_only"}.
The owner's private notes on a suggestion are never included.""",
       scopes=("suggestions:read", "site:suggest"), section="admin",
       display="site-suggestions?status=new&include=description",
       query={"status": S(), "app": S(), "priority": S(), "created_since": S(), "updated_since": S(), "q": S(),
              "include": ENUM(["description"]), "sort": ENUM(["created_at", "status_changed_at"]), "page": I(),
              "page_size": I(minimum=1, maximum=100)},
       example={"status": "new"},
       returns={"suggestions": [{"id": 14, "title": "Show time left in red", "app": "trade", "priority": "medium",
                                 "status": "new", "source": "Grok"}], "page": 1, "page_size": 25, "pages": 1,
                "total": 1, "next_page": None, "own_only": False}),
    Op("GET", "site-suggestions/counts", "countSiteSuggestions", "How many bot suggestions are in each status", """\
How many bot suggestions are in each status. Returns {"counts": {"new": 6, "declined": 9, "promoted": 3},
"total": 18} - every status is listed, 0 when empty.""",
       scopes=("suggestions:read",), section="admin",
       returns={"counts": {"new": 6, "declined": 9, "promoted": 3}, "total": 18}),
    Op("POST", "ideas", "addIdea", "Save an idea the owner told you, for Admin > Ideas", """\
Save an idea the owner told you, for them to review on Admin > Ideas.
Body: {"original_message": str (REQUIRED - exactly what the owner told you, word for word),
       "title": str (required, <=200), "description": str (optional - your cleaned-up summary),
       "app": "trade" | "resell" | "opps" | "admin" | "all",
       "category": "feature" | "improvement" | "bug" | "business_idea" | "research",
       "priority": "low" | "medium" | "high" | "urgent", "impact": "low" | "medium" | "high",
       "effort": "small" | "medium" | "large", "tags": [str, ...] (<=20, each <=50 chars),
       "links": [url, ...] (http/https only, <=20), "target_date": "YYYY-MM-DD" (optional)}
Unknown choice values fall back to the defaults (app "all", category "feature", priority/impact/effort
"medium") and are listed in "defaults_applied". Status is always "new"; you can't set status or notes.
If the title closely matches an open idea it's still saved, marked as a possible duplicate.
Returns 201 {"id": int, "status": "new", "possible_duplicate_of": int | null, "defaults_applied": [str]}.""",
       scopes=("ideas:write",), section="admin", status=201,
       body={"original_message": S(), "title": S(maxLength=200), "description": S(), "app": S(), "category": S(),
             "priority": S(), "impact": S(), "effort": S(), "tags": A(S(), maxItems=20), "links": A(S(), maxItems=20),
             "target_date": D()},
       required=("original_message", "title"),
       example={"original_message": "we should track mileage for resale pickups", "title": "Track pickup mileage"},
       returns={"id": 21, "status": "new", "possible_duplicate_of": None, "defaults_applied": []}),
    Op("GET", "ideas", "listIdeas", "Existing ideas, newest first", """\
Existing ideas, newest first, 50 per page - check before sending one. Returns
{"ideas": [{"id", "title", "app", "status", "created_at"}], "page", "pages", "total", "next_page" (int | null)}.""",
       scopes=("ideas:read",), display="ideas?page=N", section="admin", query={"page": I(minimum=1)},
       returns={"ideas": [{"id": 21, "title": "Track pickup mileage", "app": "all", "status": "new"}], "page": 1,
                "pages": 1, "total": 1, "next_page": None}),
]

SECTION_ORDER = {s.key: i for i, s in enumerate(SECTIONS)}
OPS = sorted(_OPS, key=lambda op: SECTION_ORDER[op.section])          # stable: keeps each section's order
BY_ID = {op.op_id: op for op in OPS}
FOOD_OP_IDS = ("logFood", "getFoodDay", "fixFoodEntry", "deleteFoodEntry", "recentFoods", "searchFoods")
# Routes that aren't operations: the MCP protocol endpoint (JSON-RPC - it's how the tools above are called).
NOT_OPERATIONS = ("mcp",)


# --- who can use what ------------------------------------------------------------------------------------------------

def usable_by(op, token):
    return not op.scopes or any(scope_rules.usable(token, s) for s in op.scopes)


def for_token(token):
    return [op for op in OPS if usable_by(op, token)]


# --- the shared text -------------------------------------------------------------------------------------------------

RULES = [
    "Act only on what the person asks. Before deleting anything or changing more than 5 items at once, tell them what "
    "you'll do and wait for their OK.",
    "When adding items, send an idempotency_key so retries don't create duplicates.",
    "After every change, tell them in one or two lines what you did.",
    "If a request fails, read the error message, fix the request once, and if it still fails, tell them what went wrong.",
    "Never share anything from their account with anyone else.",
    "For food logging: estimate macros per item, check foods/search for known foods first, log everything in one "
    "request, then tell them their new day totals.",
]


def _example_http(op, base_path):
    path = op.path
    for name in op.path_params:
        path = path.replace("{" + name + "}", "12")
    lines = []
    if op.method == "GET" and op.example:
        from urllib.parse import urlencode
        path += "?" + urlencode(op.example)
    lines.append(f"{op.method} {base_path}{path}")
    if not op.public:
        lines.append("Authorization: Bearer <your key>")
    if op.method != "GET" and op.example:
        lines.append("Content-Type: application/json")
        lines.append("")
        lines.append(json.dumps(op.example, indent=2))
    return "\n".join(lines)


def _scope_line(op):
    if op.public:
        return "No key needed."
    if not op.scopes:
        return "Any valid key."
    return "Scope: " + " or ".join(f"`{s}`" for s in op.scopes) + "."


def markdown(base, token=None):
    """The guide for an AI. base: https://<site>/api/bot/v1. With a token: only the endpoints it can call."""
    ops = for_token(token) if token else OPS
    base_path = API_PREFIX
    out = [f"""# Albright Labs API - a guide for AI assistants

Albright Labs is a small private website where a family and their friends keep a **Planner** (tasks and events),
**Meals** (meal plan, grocery list, recipes), **Notes**, and **Fitness** (workouts, a food diary, weigh-ins). The site
owner also has admin-only tools (Trading, Reselling, Opportunities, Ideas). An API key belongs to one person: you act
as them, see only what they can see, and what you add is theirs.

## Start here

1. Read this guide once at the start.
2. Call `GET {base}/me` to see whose key it is and exactly which endpoints it may call.
3. Use only those endpoints.

## Connecting

- Base URL: `{base}` (every path below is relative to it).
- Send the key on every request: `Authorization: Bearer <key>`. The key lives in your secret / connection settings.
  Never ask the person to paste it into a chat, and never repeat it back.
- HTTPS only. Request bodies are JSON objects (`Content-Type: application/json`); answers are JSON.
- The same endpoints as an OpenAPI 3.1 spec: `{base}/openapi.json`. As MCP tools (Streamable HTTP; sign in with OAuth
  or send the key as a Bearer token): `{base}/mcp`. Tool names are the operationIds shown below.

## Errors

Every error is JSON: `{{"error": "what went wrong"}}`, sometimes with more keys - `"required"` (the scope a 403 needs)
or `"errors": [{{"field": "items[0].calories", "message": "..."}}]` (a 400 listing each bad field).

| Status | Meaning |
| --- | --- |
| 400 | The request is invalid - the error says what to fix. |
| 401 | Missing, wrong, revoked or expired key. |
| 403 | The key lacks the scope (`"required"` names it), the owner can't open that app, API access is disabled for the owner ("API access disabled"), or the request wasn't HTTPS. |
| 404 | Not found, or not something the owner can see. |
| 405 | Wrong HTTP method. |
| 409 / 422 | The request is understood but can't be done as asked - the error explains (422s often include suggestions). |
| 429 | Too many requests - wait a minute and retry. |

## Rate limits

- {RATE_LIMIT} requests per minute per key (then 429).
- The food log: at most 60 changes (POST, PATCH, DELETE on fitness/food-entries) per hour per key.

## Rules

""" + "\n".join(f"- {rule}" for rule in RULES) + """

## Formats

- Text is stored and shown as PLAIN TEXT unless an endpoint says Markdown (HTML is shown literally). Text fields are at most 5000 characters
  (titles 200) unless stated.
- Money: decimal strings in US dollars, e.g. "12.50". Times: ISO 8601 (UTC). Dates: YYYY-MM-DD.

## Sharing

You always act as the key's owner - you see what they can see (their own things, and things a person or one of their
circles shared with them) and change only what they may edit. Things you create are shared the way the owner shares
new things in that app (their Sharing defaults). Create endpoints for Planner items, notes, recipes and workouts also
take an optional `"share": {"circle": <circle id>, "permission": "view"|"edit"}` or
`"share": {"user": "<exact username or email>", "permission": "view"|"edit"}` - checked before anything is made (400
if the owner can't share with them); the answer's "shared_with" lists who it's shared with. Only the owner can change
who something is shared with.

## WHAT YOU CANNOT DO

No endpoint exists, by design, to: place, change or cancel bids or trades; change any price; add or remove watchlist
items; create, delete or edit ledger entries; submit or send applications or proposals; read credentials, API keys,
tokens or the owner's personal profile. Suggestions are stored for the owner to review.

## Endpoints
"""]
    for section in SECTIONS:
        rows = [op for op in ops if op.section == section.key]
        if not rows:
            continue
        out.append(f"\n## {section.title}\n\n")
        if section.intro:
            out.append(section.intro + "\n")
        for op in rows:
            out.append(f"\n### `{op.heading}`\n\n")
            out.append(f"**{op.summary}.** {_scope_line(op)} operationId: `{op.op_id}`\n\n")
            out.append(op.description + "\n\n")
            out.append("Example request:\n\n```http\n" + _example_http(op, base_path) + "\n```\n\n")
            if isinstance(op.returns, str):
                out.append(f"Example response ({op.status}):\n\n```markdown\n{op.returns}\n```\n")
            elif op.returns is not None:
                out.append(f"Example response ({op.status}):\n\n```json\n{json.dumps(op.returns, indent=2)}\n```\n")
    if token is not None:
        usable = [key for key in token.scopes if scope_rules.allowed(token.owner, key)]
        out.append(f"\nYOUR TOKEN'S SCOPES: {', '.join(usable) or 'none'}\n"
                   "(Only the endpoints those scopes allow are listed above.)\n")
    return "".join(out)


# --- OpenAPI --------------------------------------------------------------------------------------------------------

ERROR_SCHEMA = O("", {"error": S(), "required": S("The scope a 403 needs"),
                      "errors": A(O("", {"field": S(example="items[0].calories"), "message": S()}))})


def _schema(props, required):
    out = {"type": "object", "properties": props}
    req = [name for name in required if name in props]
    if req:
        out["required"] = req
    return out


def _operation(op, tag):
    params = [{"name": name, "in": "path", "required": True, "schema": I()} for name in op.path_params]
    params += [{"name": name, "in": "query", "required": name in op.required, "schema": schema,
                **({"description": schema["description"]} if "description" in schema else {})}
               for name, schema in op.query.items()]
    out = {"operationId": op.op_id, "summary": op.summary, "description": op.description, "tags": [tag]}
    if params:
        out["parameters"] = params
    if op.body:
        body = _schema(op.body, op.required)
        if op.example and op.method != "GET":
            body["example"] = op.example
        out["requestBody"] = {"required": bool(body.get("required")), "content": {"application/json": {"schema": body}}}
    if op.public:
        out["security"] = []
        kind = "text/markdown" if isinstance(op.returns, str) else "application/json"
        out["responses"] = {"200": {"description": "OK", "content": {kind: {"schema": {"type": "string"} if kind == "text/markdown" else {"type": "object"}}}}}
        return out
    ok = {"description": "OK", "content": {"application/json": {"schema": {"type": "object"},
                                                                 **({"example": op.returns} if op.returns is not None else {})}}}
    error = {"description": "Error - see the message", "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}}}
    out["responses"] = {str(op.status): ok, "400": error, "401": {"description": "Missing, wrong, revoked or expired key"},
                        "403": error, "429": {"description": "Rate limited"}}
    return out


def openapi(base, ops=None, title="Albright Labs API"):
    paths = {}
    for op in ops or OPS:
        tag = SECTION_BY_KEY[op.section].title.split(" (")[0].title()
        paths.setdefault("/" + op.path, {})[op.method.lower()] = _operation(op, tag)
    return {
        "openapi": "3.1.0",
        "info": {"title": title, "version": "1.0.0",
                 "description": "Read and update one person's Albright Labs account (Planner, Meals, Notes, Fitness and "
                                f"more). Send their API key as a Bearer token. Guide for AIs: {base}/docs. "
                                "Call GET /me first to see what the key may do."},
        "servers": [{"url": base}],
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {"securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
                       "schemas": {"Error": ERROR_SCHEMA}},
    }


def food_openapi(base):
    return openapi(base, [BY_ID[i] for i in FOOD_OP_IDS], title="Albright Fitness food log")


# --- MCP tools --------------------------------------------------------------------------------------------------------

def tool_inputs(op):
    """The tool's arguments: path ids, query values and body fields side by side (no name repeats across them)."""
    props = {name: I(f"The {name.replace('_', ' ')}") for name in op.path_params}
    props.update(op.query)
    props.update(op.body)
    required = list(op.path_params) + [name for name in op.required if name in props]
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


def tool(op):
    annotations = {"title": op.summary, "readOnlyHint": not op.writes, "destructiveHint": op.method == "DELETE",
                   "openWorldHint": False}
    return {"name": op.op_id, "title": op.summary,
            "description": f"{op.summary}. {_scope_line(op)}\n\n{op.description}",
            "inputSchema": tool_inputs(op), "annotations": annotations}


def tools(token=None):
    return [tool(op) for op in (for_token(token) if token else OPS) if op.tool]
