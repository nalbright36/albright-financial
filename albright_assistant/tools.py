"""The assistant's tools: generated from the Bot API catalog (albright_bot_api.catalog.OPS) - the same names,
descriptions and inputs as the OpenAPI spec and the MCP server, so the three never drift. A tool call runs the real
API view in-process as the person (mcp.call with the assistant's token): their scopes right now, their app access,
the owner checks, the rate limit and the activity log all apply exactly as for an API key. Admin-only operations
only appear for superusers (the token only holds the scopes the person can use - access.assistant_token).

Approvals (enforced here, not just asked of the model in the prompt):
  - safe, run at once: reads, adding things, logging food / weight / workouts, checking tasks off (and back on)
  - wait for Approve: deleting anything, sharing with someone, changing settings (pausing/resuming a program),
    and editing more than EDIT_LIMIT items in one go
Undo (UNDO_MINUTES): an action chip can be undone when the change is reversible - by the API operation that reverses
it (check off <-> un-check, pause <-> resume, a program -> delete it), or by deleting the rows it created (only rows
this assistant made, or the person's own fitness log rows).
"""
import json
from datetime import timedelta

from django.apps import apps as django_apps
from django.utils import timezone

from albright_bot_api import catalog

EDIT_LIMIT = 5
UNDO_MINUTES = 10
RESULT_LIMIT = 12_000            # characters of a tool result sent back to the model
DESCRIPTION_LIMIT = 900

SECTION_APP = {"schedule": "planner", "meals": "meals", "notes": "notes", "fitness": "fitness", "overview": "",
               "account": "", "activity": ""}
SETTINGS_OPS = {"pauseProgram", "resumeProgram"}
EDIT_OPS = {"updateNote", "updateGrocery", "fixFoodEntry", "setMeal"}


# --- definitions -------------------------------------------------------------------------------------------------------

def definitions(token):
    """[{"name", "description", "parameters"}] for every operation this token may call."""
    out = []
    for op in catalog.for_token(token):
        if not op.tool or op.public:
            continue
        description = f"{op.summary}. {op.description}".replace("\n", " ")
        if len(description) > DESCRIPTION_LIMIT:
            description = description[:DESCRIPTION_LIMIT - 1] + "…"
        out.append({"name": op.op_id, "description": description, "parameters": catalog.tool_inputs(op)})
    return out


def section_notes(token):
    """The catalog's section intros for the sections these tools come from - the rules the descriptions lean on."""
    used = {op.section for op in catalog.for_token(token) if op.tool and not op.public}
    return [f"{s.title}: {' '.join(s.intro.split())}" for s in catalog.SECTIONS if s.key in used and s.intro]


# --- approvals ---------------------------------------------------------------------------------------------------------

def needs_approval(calls):
    """{call id: reason} for the calls in one reply that must wait for Approve."""
    reasons = {}
    edits = [c for c in calls if c.name in EDIT_OPS]
    for call in calls:
        op = catalog.BY_ID.get(call.name)
        if op is None:
            continue
        if op.method == "DELETE":
            reasons[call.id] = "Deletes something"
        elif call.arguments.get("share"):
            reasons[call.id] = "Shares with someone"
        elif call.name in SETTINGS_OPS:
            reasons[call.id] = "Changes a setting"
        elif call.name in EDIT_OPS and len(edits) > EDIT_LIMIT:
            reasons[call.id] = f"Edits {len(edits)} items at once"
    return reasons


def describe(call):
    """One line for the approval card: what the call will do."""
    op = catalog.BY_ID.get(call.name)
    args = call.arguments
    what = args.get("title") or args.get("name") or ""
    ident = next((f"#{args[k]}" for k in ("id", "item_id") if k in args), "")
    return " ".join(p for p in (op.summary if op else call.name, f"“{what}”" if what else "", ident) if p)


# --- running a tool -------------------------------------------------------------------------------------------------

def run(request, token, call):
    """Runs one tool call as the person. Returns (text for the model, data dict or None, is_error)."""
    from albright_bot_api import mcp
    op = catalog.BY_ID.get(call.name)
    if op is None or not op.tool or op.public or not catalog.usable_by(op, token):
        return json.dumps({"error": f"No tool named {call.name!r} is available."}), None, True
    if "_unparsed" in call.arguments:
        return json.dumps({"error": "The arguments weren't valid JSON. Send them again as a JSON object."}), None, True
    result = mcp.call(request, op, call.arguments, as_token=token)
    text = "".join(part.get("text", "") for part in result.get("content") or [])
    if len(text) > RESULT_LIMIT:
        text = text[:RESULT_LIMIT] + "… (cut)"
    return text, result.get("structuredContent"), bool(result.get("isError"))


# --- action chips and undo -----------------------------------------------------------------------------------------

def _day(value):
    from datetime import date
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return str(value)
    return f"{d:%a %b} {d.day}"


def _names(rows, key="name", limit=4):
    names = [str(r.get(key)) for r in rows or [] if isinstance(r, dict) and r.get(key)]
    return ", ".join(names[:limit]) + (f" +{len(names) - limit} more" if len(names) > limit else "")


def summary(call, data):
    """The chip's text: "Added task: Call dentist - Thu Nov 19"."""
    a, d = call.arguments, data if isinstance(data, dict) else {}
    title = d.get("title") or a.get("title") or a.get("name") or ""
    n = call.name
    if n == "addScheduleItem":
        kind = d.get("kind") or a.get("kind") or "task"
        return f"Added {kind}: {title}" + (f" - {_day(a.get('date'))}" if a.get("date") else "")
    if n in ("completeScheduleItem", "completeChecklistItem"):
        return f"Checked off: {title or 'item #' + str(a.get('id'))}"
    if n in ("uncompleteScheduleItem", "uncompleteChecklistItem"):
        return f"Un-checked: {title or 'item #' + str(a.get('id'))}"
    if n == "addGroceries":
        return f"Added to groceries: {_names(d.get('items')) or a.get('name', '')}"
    if n in ("logFood", "logFoodByName"):
        entries = d.get("entries") or []
        cal = sum(float(e.get("calories") or 0) for e in entries if isinstance(e, dict))
        return f"Logged food: {_names(entries) or 'entry'}" + (f" ({cal:.0f} cal)" if cal else "")
    if n == "logWeight":
        return f"Logged weight: {d.get('weight') or a.get('weight')} {d.get('unit') or a.get('unit') or ''}".strip()
    if n == "logWorkout":
        return f"Logged workout: {d.get('name') or 'Workout'}"
    if n == "addNote":
        return f"Saved note: {title}"
    if n == "updateNote":
        return f"Updated note: {title or '#' + str(a.get('id'))}"
    if n == "setMeal":
        return f"Planned {a.get('slot', 'meal')} {_day(a.get('date'))}: {title}"
    if n == "saveRecipe":
        return f"Saved recipe: {title}"
    if n == "addMealIdea":
        return f"Added meal idea: {title}"
    if n == "updateGrocery":
        return f"Updated grocery: {d.get('name') or '#' + str(a.get('id'))}"
    op = catalog.BY_ID.get(n)
    return (op.summary if op else n)[:200]


# (op that made it) -> how to reverse it. "ids": where the created rows' ids are in the result.
DELETE_UNDO = {
    "addScheduleItem": ("albright_planner.ScheduleItem", lambda d: [d.get("id")]),
    "addNote": ("albright_notes.Note", lambda d: [d.get("id")]),
    "addGroceries": ("albright_meals.GroceryItem",
                     lambda d: [r.get("id") for r in d.get("items") or [] if isinstance(r, dict) and not r.get("merged")]),
    "addMealIdea": ("albright_meals.MealIdea", lambda d: [d.get("id")]),
    "saveRecipe": ("albright_meals.Recipe", lambda d: [d.get("id")] if d.get("created") else []),
    "setMeal": ("albright_meals.MealPlanEntry", lambda d: [d.get("id")] if d.get("created") else []),
    "logFood": ("albright_fitness.FoodLogEntry", lambda d: [e.get("id") for e in d.get("entries") or [] if isinstance(e, dict)]),
    "logWeight": ("albright_fitness.WeightEntry", lambda d: [] if d.get("replaced") else [d.get("id")]),
    "logWorkout": ("albright_fitness.Workout", lambda d: [d.get("id")]),
}
INVERSE_OPS = {"completeScheduleItem": "uncompleteScheduleItem", "uncompleteScheduleItem": "completeScheduleItem",
               "completeChecklistItem": "uncompleteChecklistItem", "uncompleteChecklistItem": "completeChecklistItem",
               "pauseProgram": "resumeProgram", "resumeProgram": "pauseProgram"}
# Who may have rows deleted by undo: rows this assistant's token made (bot_token), or the person's own (owner).
GUARD_FIELD = {"albright_fitness.FoodLogEntry": "owner", "albright_fitness.WeightEntry": "owner",
               "albright_fitness.Workout": "owner"}


def undo_spec(call, data):
    d = data if isinstance(data, dict) else {}
    if call.name in INVERSE_OPS:
        return {"kind": "op", "op": INVERSE_OPS[call.name], "args": dict(call.arguments)}
    if call.name == "createProgram" and d.get("id"):
        return {"kind": "op", "op": "deleteProgram", "args": {"id": d["id"]}}
    if call.name == "updateGrocery" and isinstance(call.arguments.get("checked"), bool) and \
            set(call.arguments) <= {"id", "checked", "space"}:
        return {"kind": "op", "op": "updateGrocery", "args": {**call.arguments, "checked": not call.arguments["checked"]}}
    if call.name in DELETE_UNDO:
        model, ids_of = DELETE_UNDO[call.name]
        ids = [i for i in ids_of(d) if isinstance(i, int)]
        if ids:
            return {"kind": "delete", "model": model, "ids": ids}
    return {}


def app_of(name):
    op = catalog.BY_ID.get(name)
    return SECTION_APP.get(op.section, op.section) if op else ""


def record(run_row, message, call, data):
    """Saves the action chip for a successful change."""
    from .models import Action
    spec = undo_spec(call, data)
    return Action.objects.create(chat=run_row.chat, run=run_row, user=run_row.user, message=message, op=call.name,
                                 app=app_of(call.name), summary=summary(call, data)[:200], undo=spec,
                                 undo_until=timezone.now() + timedelta(minutes=UNDO_MINUTES) if spec else None)


class UndoError(Exception):
    pass


def undo(request, action, token):
    """Reverses an action (raises UndoError with a plain reason)."""
    if not action.can_undo:
        raise UndoError("This can't be undone any more.")
    spec = action.undo
    if spec.get("kind") == "op":
        from .providers import ToolCall
        text, _data, is_error = run(request, token, ToolCall("undo", spec["op"], spec.get("args") or {}))
        if is_error:
            raise UndoError("Couldn't undo it: " + text[:200])
    elif spec.get("kind") == "delete":
        model = django_apps.get_model(spec["model"])
        guard = GUARD_FIELD.get(spec["model"])
        rows = model.objects.filter(pk__in=spec.get("ids") or [])
        rows = rows.filter(owner=action.user) if guard == "owner" else rows.filter(bot_token=token)
        if not rows.exists():
            raise UndoError("It's already gone.")
        rows.delete()
    else:
        raise UndoError("This can't be undone.")
    action.undone_at = timezone.now()
    action.save(update_fields=["undone_at"])
