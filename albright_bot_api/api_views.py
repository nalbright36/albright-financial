"""Bot API v1 endpoints (/api/bot/v1/...). Read some data, leave
suggestions - nothing here bids, changes a max bid, touches the watchlist,
creates/deletes ledger entries, changes a price, submits an application,
trades, or reads credentials, keys, tokens or my profile. Every write goes
to this app's own tables (models.py)."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .auth import ApiError, bot_endpoint, json_body
from .models import MAX_TEXT, SCOPES, InventorySuggestion, LotNote, OpportunityNote, SiteSuggestion, TradingNote

UNSOLD = ("holding", "partially_sold")
NEW_OPPORTUNITY_DAYS = 7


def _text(data, key, required=True, limit=MAX_TEXT):
    value = data.get(key)
    if value in (None, ""):
        if required:
            raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
        return ""
    if not isinstance(value, str):
        raise ApiError(400, f'"{key}" must be a string.', f"rejected: {key} not a string")
    value = value.strip()
    if len(value) > limit:
        raise ApiError(400, f'"{key}" is longer than {limit} characters.', f"rejected: {key} too long")
    if required and not value:
        raise ApiError(400, f'"{key}" is required.', f"rejected: missing {key}")
    return value


def _money(value):
    return f"{value:.2f}" if value is not None else None


def _iso(value):
    return value.isoformat() if value else None


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------

DOCS = """ALBRIGHT BOT API v1 - read data, leave suggestions. You can never take actions - except in Meals,
whose meals:write endpoints add to the household's plan, grocery list, ideas and recipes directly (labeled
"Recipe Bot" on the site).

BASE URL: <site>/api/bot/v1/
AUTH: send "Authorization: Bearer <token>" on every request, over HTTPS.
  401 = missing, wrong or revoked token.  403 = the token lacks the endpoint's scope (or the request wasn't HTTPS).
  429 = more than 60 requests in a minute for this token - wait and retry.  405 = wrong HTTP method.
  400 = invalid input; the JSON body {"error": "..."} says what's wrong.
REQUESTS: POST bodies are JSON objects (Content-Type: application/json). Responses are JSON.
TEXT LIMITS: every text field is at most 5000 characters (titles 200). Text is stored and shown as PLAIN TEXT -
  HTML or Markdown is displayed literally, not rendered.
MONEY: decimal strings in US dollars, e.g. "12.50". TIMES: ISO 8601 (UTC). DATES: YYYY-MM-DD.

WHAT YOU CANNOT DO (no endpoint exists for it, by design): place, change or cancel bids or trades; change a
max bid; add or remove watchlist items; create, delete or edit ledger entries; change any price; submit or send
applications or proposals; read credentials, API keys, tokens or the owner's personal profile.
Your suggestions are stored for the owner to review; a price suggestion changes nothing until the owner accepts it.

ENDPOINTS
---------
GET  docs
  This text. Any valid token.

POST trading/notes                                   scope: trading:notes
  Body: {"date": "YYYY-MM-DD" (optional, default today), "title": str (required, <=200), "text": str (required),
  "strategy": str (optional - one of the owner's strategies, by name or by one of its prefixes, any capitalization)}
  Shown on the owner's Grok Bot page under "Bot notes", newest first, and on that strategy's page when tagged.
  Example: {"title": "Closed SPY spread", "text": "Took profit at 50%.", "strategy": "eng"}
  Returns 201 {"id": int, "strategy": name or null}. An unknown strategy is a 400 listing the valid ones.

GET  trading/strategies                               scope: trading:notes
  The owner's bot strategies, so a bot can confirm its prefix: {"strategies": [{"name": str, "prefixes": [str],
  "status": "active"|"paused"|"retired"}]}, in the owner's display order. An order counts toward a strategy when
  its client_order_id starts with one of its prefixes (any capitalization; the longest matching prefix wins).

GET  reseller/lots                                    scope: reseller:read
  Today's live auction lots worth a look: scanner candidates and "Check by hand" lots (low-confidence parses that
  still fit under the max bid). Returns {"lots": [...]}, soonest-ending first. Each lot:
    id, list ("candidate" | "check_by_hand"), title, url, source, category, current_bid, max_bid (the scanner's
    computed max), headroom (max_bid - current_bid), ends_at, image_url, confidence ("high"|"medium"|"low"),
    flags (scanner notes, e.g. "weight_mismatch", "key_date_verify"), matched_keyword.

POST reseller/lots/<id>/notes                         scope: reseller:suggest
  Body: {"text": str}. A second-opinion note shown in that lot's expandable row, labeled "Grok". Returns 201 {"id"}.

GET  reseller/inventory                               scope: reseller:read
  The owner's unsold ledger items. Returns {"items": [...]}. Each item:
    id, item, ledger_type ("resale"|"personal"), status, cost, list_price, platform, listed_at, days_listed
    (null if not listed), next_markdown {label, step_days, due (bool), due_date} or null.

POST reseller/inventory/<id>/suggestions              scope: reseller:suggest
  Body: {"suggested_price": "12.50" (optional), "listing_draft": str (optional), "reason": str (optional)} -
  at least one of suggested_price / listing_draft. Saved as PENDING on the item's page; the owner accepts or
  dismisses it. Returns 201 {"id": int, "status": "pending"}.

GET  opportunities/new                                scope: opportunities:read
  Opportunities found in the last 7 days that aren't blocked by the safety check or excluded (spending above
  budget). Jobs from job boards whose terms forbid passing listings on (Remotive, Remote OK, Himalayas, Jobicy,
  Arbeitnow) are never included. Returns {"opportunities": [...]}. Each:
    id, type ("crypto"|"income"), title, url, discovered_at, reward_usd, reward_type, pay_type, pay_rate_usd,
    hours_estimate, ev_mid, ev_per_hour, confidence, risk_score, safety_verdict ("ok"|"warn"), safety_reasons,
    deadline, application_deadline, skills_required, skill_match, location_requirement, status,
    text (the posting text, up to 3000 characters).

POST opportunities/<id>/notes                         scope: opportunities:suggest
  Body: {"text": str}. Shown on the opportunity's page labeled "Grok". Returns 201 {"id": int}.

POST opportunities/<id>/proposal-draft                scope: opportunities:suggest
  Body: {"text": str}. A proposal draft shown on the opportunity's page labeled "Grok". It is never sent
  anywhere - the owner decides. Returns 201 {"id": int}.

POST site-suggestions                                 scope: site:suggest
  A website improvement idea for the owner's Admin inbox.
  Body: {"app": "trade" | "resell" | "opps" | "all" (required), "area": str (optional, <=100, e.g. "Watchlist page"),
         "title": str (required, <=200), "description": str (required - what to change and why),
         "priority": "low" | "medium" | "high" (optional, default "medium")}
  The owner reviews it (New -> Accepted / Declined -> Done); nothing changes on the site by itself.
  Returns 201 {"id": int, "status": "new"}.

POST ideas                                            scope: ideas:write
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
  Returns 201 {"id": int, "status": "new", "possible_duplicate_of": int | null, "defaults_applied": [str]}.

GET  ideas?page=N                                     scope: ideas:read
  Existing ideas, newest first, 50 per page - check before sending one. Returns
  {"ideas": [{"id", "title", "app", "status", "created_at"}], "page", "pages", "total", "next_page" (int | null)}.

MEALS (the household's meal planning - you are the "Recipe Bot")
------------------------------------------------------------------
Slots: "breakfast" | "lunch" | "dinner" | "snack" - one meal per day per slot. Aisles: "produce" | "meat" |
"dairy" | "bakery" | "pantry" | "frozen" | "household" | "other". Quantities: a number (2, 0.5) or a string
("1 1/2"), up to 100000. "added_by" is "Recipe Bot" for what you created, else the person's name.

GET  meals/plan?start=YYYY-MM-DD&end=YYYY-MM-DD       scope: meals:read
  The plan between two dates (default: this week, Monday to Sunday; at most 62 days). Returns
  {"start", "end", "entries": [{"id", "date", "slot", "title", "recipe_id" (int | null), "status"
  ("planned"|"cooked"|"skipped"), "notes", "added_by"}]}.

POST meals/plan                                       scope: meals:write
  Set a day's slot - replaces whatever is planned there. Body: {"date": "YYYY-MM-DD" (required), "slot" (required),
  "recipe_id": int (optional), "title": str (<=200; required without recipe_id), "notes": str (optional)}.
  Returns 201 (new) or 200 (replaced) {"id", "created": bool, ...the entry}. 404 if recipe_id doesn't exist.

DELETE meals/plan/<id>                                scope: meals:write
  Remove a plan entry the Recipe Bot created (403 for anyone else's). Returns {"deleted": id}.

GET  meals/groceries                                  scope: meals:read
  The shared grocery list. Returns {"items": [{"id", "name", "quantity" (str | null), "unit", "aisle", "note",
  "checked", "added_by"}]}.

POST meals/groceries                                  scope: meals:write
  Add one item {"name": str (required, <=120), "quantity", "unit" (<=20), "aisle" (optional - guessed from the
  name), "note" (<=200)} or many: {"items": [ ...up to 100 of those ]}. An item merges into an UNCHECKED line
  with the same name and unit (quantities added). Returns 201 {"items": [{"id", "merged": bool, ...}]}.

PATCH meals/groceries/<id>                            scope: meals:write
  Body: any of {"quantity", "note", "checked": true|false}. Returns the item.

GET  meals/ideas                                      scope: meals:read
  Meal ideas, newest first (up to 200). Returns {"ideas": [{"id", "title", "notes", "link", "status"
  ("new"|"planned"|"declined"), "added_by", "created_at"}]}.

POST meals/ideas                                      scope: meals:write
  Body: {"title": str (required, <=200), "notes": str (optional), "link": http(s) URL (optional)}.
  Returns 201 {"id", ...}.

GET  meals/recipes?q=<words>                          scope: meals:read
  Recipes, most recently changed first (up to 200; q filters by title). Each: {"id", "title", "description",
  "ingredients": [{"name", "quantity", "unit", "note"}], "steps": [str], "prep_minutes", "cook_minutes",
  "servings", "tags", "source_url", "image_url", "notes", "added_by", "updated_at"}.

POST meals/recipes                                    scope: meals:write
  Create a recipe, or update the one with the same title (case-insensitive) - only the fields you send change.
  Body: {"title": str (required, <=200), "description", "notes" (<=5000), "ingredients": [{"name" (required,
  <=120), "quantity" (<=20), "unit" (<=20), "note" (<=200)} or "2 lb chicken thighs", ...] (<=100),
  "steps": [str (<=2000)] (<=100), "prep_minutes" / "cook_minutes" (0-2880), "servings" (1-100),
  "tags": [str (<=50)] (<=20), "source_url" / "image_url" (http(s), <=500)}.
  Returns 201 (created) or 200 (updated) {"id": int, "created": bool}.

POST meals/recipes/import                             scope: meals:write
  Import a recipe from a link. Only that one page is read (never anything it links to): its recipe data
  (schema.org JSON-LD or microdata), or - if it has none - the AI reads the page's visible text, keeping only
  what is on the page; those recipes are marked "needs review". A link that's already saved (tracking
  parameters like utm_* and #fragments ignored) returns the saved recipe instead of a copy.
  Body: {"url": http(s) URL (required, <=2000), "add_to_plan": {"date": "YYYY-MM-DD", "slot":
  "breakfast"|"lunch"|"dinner"|"snack"} (optional), "add_to_groceries": bool (optional - adds the ingredients
  not already on the list from this recipe, merged with unchecked items of the same name and unit)}.
  Example: {"url": "https://www.example.com/recipes/chili", "add_to_plan": {"date": "2026-11-02",
  "slot": "dinner"}, "add_to_groceries": true}
  Returns 201 (new) or 200 (already saved) {"id": int, "title": str, "status": "created"|"already_saved"|
  "needs_review", "url": the recipe's page, "plan": {...} (with add_to_plan), "groceries": {"added", "merged",
  "already_listed"} (with add_to_groceries)}.
  Errors ({"error": "..."}): 400 not a public http(s) page; 422 "No recipe found on that page." (or the page is
  over 3 MB); 502 the page couldn't be loaded; 504 it took over 10 seconds; 503 no recipe data and the AI
  reader is unavailable or over its daily limit.
"""


@bot_endpoint()
def docs(request, token):
    scopes = ", ".join(token.scopes) or "none"
    return DOCS + f"\nYOUR TOKEN'S SCOPES: {scopes}\nALL SCOPES: " + ", ".join(k for k, _ in SCOPES) + "\n", 200, "read docs"


# ---------------------------------------------------------------------------
# Trading
# ---------------------------------------------------------------------------

def _owner_strategy(owner, value):
    """The owner's BotStrategy named `value`, or else the one with `value` as a prefix (any capitalization)."""
    from albright_trading_app.models import BotStrategy
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, '"strategy" must be a strategy name or prefix.', "rejected: bad strategy")
    wanted = value.strip().casefold()
    strategies = list(BotStrategy.objects.filter(user=owner))
    match = next((s for s in strategies if s.name.casefold() == wanted), None) \
        or next((s for s in strategies if wanted in [p.casefold() for p in s.prefixes]), None)
    if match is None:
        names = ", ".join(s.name for s in strategies) or "none yet"
        raise ApiError(400, f'No strategy "{value.strip()[:80]}". Your strategies: {names}.', "rejected: unknown strategy")
    return match


@bot_endpoint("trading:notes", methods=("POST",))
def trading_notes(request, token):
    data = json_body(request)
    title = _text(data, "title", limit=200)
    text = _text(data, "text")
    raw_date = data.get("date")
    try:
        note_date = date.fromisoformat(raw_date) if raw_date else timezone.localdate()
    except (TypeError, ValueError) as exc:
        raise ApiError(400, '"date" must be YYYY-MM-DD.', "rejected: bad date") from exc
    strategy = _owner_strategy(token.owner, data["strategy"]) if data.get("strategy") is not None else None
    note = TradingNote.objects.create(owner=token.owner, token=token, date=note_date, title=title, text=text,
                                      strategy=strategy)
    return ({"id": note.pk, "strategy": strategy.name if strategy else None}, 201,
            f"trading note: {title[:80]}" + (f" [{strategy.name[:40]}]" if strategy else ""))


@bot_endpoint("trading:notes")
def trading_strategies(request, token):
    from albright_trading_app.models import BotStrategy
    rows = [{"name": s.name, "prefixes": list(s.prefixes), "status": s.status}
            for s in BotStrategy.objects.filter(user=token.owner)]
    return {"strategies": rows}, 200, f"listed {len(rows)} strategies"


# ---------------------------------------------------------------------------
# Reseller
# ---------------------------------------------------------------------------

def _lot_json(evaluation, which):
    lot = evaluation.lot
    return {
        "id": lot.pk, "list": which, "title": lot.title, "url": lot.url, "source": lot.source,
        "category": evaluation.category, "current_bid": _money(lot.current_price), "max_bid": _money(evaluation.max_bid),
        "headroom": _money(evaluation.headroom), "ends_at": _iso(lot.end_time), "image_url": lot.image_url or "",
        "confidence": evaluation.confidence, "flags": list(evaluation.flags or []),
        "matched_keyword": lot.matched_keyword or "",
    }


@bot_endpoint("reseller:read")
def reseller_lots(request, token):
    from albright_reselling_app.scanner.dashboard import check_by_hand_evaluations
    from albright_reselling_app.scanner_models import LotEvaluation
    now = timezone.now()
    candidates = (LotEvaluation.objects.select_related("lot")
                  .filter(is_candidate=True, lot__end_time__gt=now).order_by("lot__end_time"))
    check, _hidden = check_by_hand_evaluations(now, set())
    lots = [_lot_json(ev, "candidate") for ev in candidates]
    seen = {lot["id"] for lot in lots}
    lots += [_lot_json(ev, "check_by_hand") for ev in check if ev.lot_id not in seen]
    lots.sort(key=lambda l: l["ends_at"] or "")
    return {"lots": lots}, 200, f"listed {len(lots)} lots"


@bot_endpoint("reseller:suggest", methods=("POST",))
def reseller_lot_note(request, token, lot_id):
    from albright_reselling_app.scanner_models import SourcedLot
    lot = SourcedLot.objects.filter(pk=lot_id).first()
    if lot is None:
        raise ApiError(404, "No such lot.", f"rejected: lot {lot_id} not found")
    note = LotNote.objects.create(lot=lot, token=token, text=_text(json_body(request), "text"))
    return {"id": note.pk}, 201, f"note on lot {lot.pk}"


def _owned_unsold(token, entry_id):
    from albright_reselling_app.models import LedgerEntry
    entry = LedgerEntry.objects.filter(pk=entry_id, owner=token.owner, status__in=UNSOLD).first()
    if entry is None:
        raise ApiError(404, "No such unsold item.", f"rejected: item {entry_id} not found")
    return entry


@bot_endpoint("reseller:read")
def reseller_inventory(request, token):
    from albright_reselling_app.models import LedgerEntry
    from albright_reselling_app.selling import markdown_status
    today = timezone.localdate()
    items = []
    for entry in LedgerEntry.objects.filter(owner=token.owner, status__in=UNSOLD).order_by("pk"):
        status = markdown_status(entry, today=today)
        items.append({
            "id": entry.pk, "item": entry.item, "ledger_type": entry.ledger_type, "status": entry.status,
            "cost": _money(entry.cost), "list_price": _money(entry.list_price), "platform": entry.platform or "",
            "listed_at": _iso(entry.listed_at), "days_listed": status["days"] if status["listed"] else None,
            "next_markdown": ({"label": status["label"], "step_days": status["step"], "due": status["due"],
                               "due_date": _iso(status["due_date"])} if status["listed"] and status["label"] else None),
        })
    return {"items": items}, 200, f"listed {len(items)} inventory items"


@bot_endpoint("reseller:suggest", methods=("POST",))
def reseller_inventory_suggestion(request, token, entry_id):
    entry = _owned_unsold(token, entry_id)
    data = json_body(request)
    price = None
    if data.get("suggested_price") not in (None, ""):
        try:
            price = Decimal(str(data["suggested_price"])).quantize(Decimal("0.01"))
        except (InvalidOperation, ValueError) as exc:
            raise ApiError(400, '"suggested_price" must be a number like "12.50".', "rejected: bad price") from exc
        if not (Decimal("0") < price < Decimal("10000000")):
            raise ApiError(400, '"suggested_price" must be positive.', "rejected: bad price")
    draft = _text(data, "listing_draft", required=False)
    reason = _text(data, "reason", required=False)
    if price is None and not draft:
        raise ApiError(400, 'Send "suggested_price" and/or "listing_draft".', "rejected: empty suggestion")
    suggestion = InventorySuggestion.objects.create(entry=entry, token=token, suggested_price=price,
                                                    listing_draft=draft, reason=reason)
    what = " + ".join(filter(None, [f"price ${price}" if price else "", "listing draft" if draft else ""]))
    return {"id": suggestion.pk, "status": "pending"}, 201, f"suggestion for item {entry.pk}: {what}"


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------

def _visible_opportunities():
    from albright_crypto_app.models import Opportunity, Source
    return (Opportunity.objects.exclude(safety_verdict="block").filter(excluded=False, duplicate_of__isnull=True)
            .exclude(source__kind__in=list(Source.ATTRIBUTED_KINDS)))   # their terms forbid passing jobs on


@bot_endpoint("opportunities:read")
def opportunities_new(request, token):
    since = timezone.now() - timedelta(days=NEW_OPPORTUNITY_DAYS)
    rows = []
    for o in _visible_opportunities().filter(discovered_at__gte=since).order_by("-discovered_at"):
        rows.append({
            "id": o.pk, "type": o.opportunity_type, "title": o.title, "url": o.url, "discovered_at": _iso(o.discovered_at),
            "reward_usd": _money(o.reward_usd), "reward_type": o.reward_type, "pay_type": o.pay_type,
            "pay_rate_usd": _money(o.pay_rate_usd), "hours_estimate": _money(o.hours_estimate),
            "ev_mid": _money(o.ev_mid), "ev_per_hour": _money(o.ev_per_hour), "confidence": o.confidence,
            "risk_score": o.risk_score, "safety_verdict": o.safety_verdict, "safety_reasons": list(o.safety_reasons or []),
            "deadline": _iso(o.deadline), "application_deadline": _iso(o.application_deadline),
            "skills_required": list(o.skills_required or []), "skill_match": o.skill_match,
            "location_requirement": o.location_requirement, "status": o.status, "text": (o.raw_text or "")[:3000],
        })
    return {"opportunities": rows}, 200, f"listed {len(rows)} opportunities"


def _opportunity_write(request, token, opportunity_id, kind):
    opp = _visible_opportunities().filter(pk=opportunity_id).first()
    if opp is None:
        raise ApiError(404, "No such opportunity.", f"rejected: opportunity {opportunity_id} not found")
    note = OpportunityNote.objects.create(opportunity=opp, token=token, kind=kind, text=_text(json_body(request), "text"))
    return {"id": note.pk}, 201, f"{'proposal draft' if kind == 'proposal' else 'note'} on opportunity {opp.pk}"


@bot_endpoint("opportunities:suggest", methods=("POST",))
def opportunity_note(request, token, opportunity_id):
    return _opportunity_write(request, token, opportunity_id, "note")


@bot_endpoint("opportunities:suggest", methods=("POST",))
def opportunity_proposal(request, token, opportunity_id):
    return _opportunity_write(request, token, opportunity_id, "proposal")


# ---------------------------------------------------------------------------
# Site suggestions (Admin inbox)
# ---------------------------------------------------------------------------

@bot_endpoint("site:suggest", methods=("POST",))
def site_suggestions(request, token):
    data = json_body(request)
    app = data.get("app")
    if app not in dict(SiteSuggestion.APPS):
        raise ApiError(400, '"app" must be one of: trade, resell, opps, all.', "rejected: bad app")
    priority = data.get("priority") or "medium"
    if priority not in dict(SiteSuggestion.PRIORITIES):
        raise ApiError(400, '"priority" must be low, medium or high.', "rejected: bad priority")
    suggestion = SiteSuggestion.objects.create(
        token=token, app=app, area=_text(data, "area", required=False, limit=100),
        title=_text(data, "title", limit=200), description=_text(data, "description"), priority=priority)
    return {"id": suggestion.pk, "status": "new"}, 201, f"site suggestion ({app}): {suggestion.title[:80]}"


# ---------------------------------------------------------------------------
# Ideas (Admin > Ideas)
# ---------------------------------------------------------------------------

IDEAS_PAGE_SIZE = 50
MAX_TAGS = MAX_LINKS = 20


def _choice(data, key, choices, applied):
    from albright_control.models import Idea
    value = data.get(key)
    if value in dict(choices):
        return value
    if value not in (None, ""):
        applied.append(key)
    return Idea.DEFAULTS[key]


def _string_list(data, key, limit, item_limit, keep=lambda v: True):
    value = data.get(key) or []
    if not isinstance(value, list):
        raise ApiError(400, f'"{key}" must be a list.', f"rejected: {key} not a list")
    items = []
    for item in value:
        if isinstance(item, str) and item.strip() and len(item.strip()) <= item_limit and keep(item.strip()):
            if item.strip() not in items:
                items.append(item.strip())
    return items[:limit]


@bot_endpoint("ideas:write", methods=("POST",))
def ideas_create(request, token):
    from albright_control.ideas import find_duplicate
    from albright_control.models import AuditEntry, Idea
    from .links_check import is_http_url
    data = json_body(request)
    original = _text(data, "original_message")
    title = _text(data, "title", limit=200)
    applied = []
    fields = {key: _choice(data, key, choices, applied) for key, choices in (
        ("app", Idea.APPS), ("category", Idea.CATEGORIES), ("priority", Idea.PRIORITIES),
        ("impact", Idea.IMPACTS), ("effort", Idea.EFFORTS))}
    target = data.get("target_date")
    try:
        target_date = date.fromisoformat(target) if target else None
    except (TypeError, ValueError) as exc:
        raise ApiError(400, '"target_date" must be YYYY-MM-DD.', "rejected: bad target_date") from exc
    duplicate = find_duplicate(title)
    idea = Idea.objects.create(
        title=title, description=_text(data, "description", required=False), original_message=original,
        tags=_string_list(data, "tags", MAX_TAGS, 50), links=_string_list(data, "links", MAX_LINKS, 500, is_http_url),
        target_date=target_date, status="new", source="grok", token=token, possible_duplicate_of=duplicate, **fields)
    AuditEntry.objects.create(action="idea_created", details=f'#{idea.pk} "{title[:80]}" by {token.name} (Bot API)'
                              + (f" - possible duplicate of #{duplicate.pk}" if duplicate else ""))
    return ({"id": idea.pk, "status": "new", "possible_duplicate_of": duplicate.pk if duplicate else None,
             "defaults_applied": applied}, 201,
            f"idea #{idea.pk}: {title[:80]}" + (f" (possible duplicate of #{duplicate.pk})" if duplicate else ""))


@bot_endpoint("ideas:read")
def ideas_list(request, token):
    from albright_control.models import Idea
    try:
        page = max(1, int(request.GET.get("page") or 1))
    except ValueError as exc:
        raise ApiError(400, '"page" must be a number.', "rejected: bad page") from exc
    qs = Idea.objects.order_by("-created_at", "-pk")
    total = qs.count()
    pages = max(1, -(-total // IDEAS_PAGE_SIZE))
    rows = [{"id": i.pk, "title": i.title, "app": i.app, "status": i.status, "created_at": _iso(i.created_at)}
            for i in qs[(page - 1) * IDEAS_PAGE_SIZE: page * IDEAS_PAGE_SIZE]]
    return ({"ideas": rows, "page": page, "pages": pages, "total": total,
             "next_page": page + 1 if page < pages else None}, 200, f"listed {len(rows)} ideas (page {page})")


@csrf_exempt
def ideas(request):
    """One path, two scopes: POST creates (ideas:write), anything else lists (ideas:read, GET only)."""
    return ideas_create(request) if request.method == "POST" else ideas_list(request)
