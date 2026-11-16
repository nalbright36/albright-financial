"""Bot API v1 endpoints (/api/bot/v1/...). Read some data, leave
suggestions - nothing here bids, changes a max bid, touches the watchlist,
creates/deletes ledger entries, changes a price, submits an application,
trades, or reads credentials, keys, tokens or my profile. Every write goes
to this app's own tables (models.py)."""
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation

from django.db.models import Count, F, Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime
from django.views.decorators.csrf import csrf_exempt

from . import scopes as scope_rules
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

# The guide, the OpenAPI spec and /me are all built from catalog.py (one description of every endpoint).

def _base(request):
    from .connect import api_base
    return api_base(request)


@csrf_exempt
def docs(request):
    """The guide for an AI, as markdown. No key needed. With a key, only what that key can call (a bad key: 401)."""
    from django.http import HttpResponse

    from . import catalog
    if request.META.get("HTTP_AUTHORIZATION"):
        return _docs_for_key(request)
    return HttpResponse(catalog.markdown(_base(request)), content_type="text/markdown; charset=utf-8")


@bot_endpoint()
def _docs_for_key(request, token):
    from . import catalog
    return catalog.markdown(_base(request), token), 200, "read docs"


def openapi(request):
    """GET openapi.json - every endpoint, OpenAPI 3.1. No key needed (no user data in it)."""
    from django.http import JsonResponse

    from . import catalog
    return JsonResponse(catalog.openapi(_base(request)), json_dumps_params={"indent": 2})


@bot_endpoint()
def me(request, token):
    """Who this key acts as, and what it may call - so an AI can check before it acts."""
    from . import catalog
    usable = [key for key in token.scopes if scope_rules.allowed(token.owner, key)]
    endpoints = [{"method": op.method, "path": op.path, "operation_id": op.op_id, "summary": op.summary}
                 for op in catalog.for_token(token) if not op.public]
    return ({"username": token.owner.get_username(), "key_name": token.name, "scopes": usable,
             "apps": [{"app": name, "access": kind} for name, kind in scope_rules.by_app(usable)],
             "endpoints": endpoints, "expires_at": _iso(token.expires_at)}, 200, "read me")


@bot_endpoint()
def my_storage(request, token):
    from albright_labs import storage
    u = storage.get_storage_usage(token.owner)
    return ({"used_bytes": u["used_bytes"], "limit_bytes": u["limit_bytes"], "percent": u["percent"],
             "remaining_bytes": u["remaining_bytes"], "tier": u["tier_name"]}, 200, "read storage")


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

def _visible_opportunities(token):
    """Openings the token owner added or a bot pushed to their inbox (never another person's, never unowned)."""
    from albright_crypto_app.models import Opportunity, Source
    return (Opportunity.objects.exclude(safety_verdict="block").filter(excluded=False, duplicate_of__isnull=True)
            .filter(owner=token.owner)
            .exclude(source__kind__in=list(Source.ATTRIBUTED_KINDS)))   # their terms forbid passing jobs on


@bot_endpoint("opportunities:read")
def opportunities_new(request, token):
    since = timezone.now() - timedelta(days=NEW_OPPORTUNITY_DAYS)
    rows = []
    for o in _visible_opportunities(token).filter(discovered_at__gte=since).order_by("-discovered_at"):
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
    opp = _visible_opportunities(token).filter(pk=opportunity_id).first()
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


def _when(data, key):
    """An optional ISO date-time; a value without a zone is taken in the site's zone."""
    raw = data.get(key)
    if raw in (None, ""):
        return None
    try:
        value = datetime.fromisoformat(str(raw).strip())
    except ValueError as exc:
        raise ApiError(400, f'"{key}" must be an ISO date-time.', f"rejected: bad {key}") from exc
    if timezone.is_naive(value):
        value = timezone.make_aware(value, timezone.get_current_timezone())
    return value


@bot_endpoint("opportunities:write", methods=("POST",))
def opportunities_create(request, token):
    """A job or freelance opening for the token owner's Opps inbox (owner set - nobody else sees it). Its own
    "My bot" source per token; the same url again (ignoring a trailing slash, #fragment and case) is the same
    opening. Safety check and skill fit run at once, without the AI."""
    from albright_crypto_app.models import PAY_TYPES, WORK_TYPES, Opportunity, Source
    from albright_crypto_app.pipeline import extract_and_score

    from .links_check import is_http_url
    data = json_body(request)
    url = _text(data, "url", limit=1000)
    if not is_http_url(url):
        raise ApiError(400, '"url" must be an http(s) link.', "rejected: bad url")
    title = _text(data, "title", limit=500)
    work_types = [k for k, _ in WORK_TYPES if k != "crypto"]
    work_type = data.get("work_type") or "other"
    if work_type not in work_types:
        raise ApiError(400, f'"work_type" must be one of: {", ".join(work_types)}.', "rejected: bad work_type")
    pay_type = data.get("pay_type") or None
    if pay_type and pay_type not in dict(PAY_TYPES):
        raise ApiError(400, '"pay_type" must be hourly, fixed, prize or bonus.', "rejected: bad pay_type")
    rate = data.get("pay_rate_usd")
    try:
        rate = Decimal(str(rate)).quantize(Decimal("0.01")) if rate not in (None, "") else None
    except (InvalidOperation, ValueError) as exc:
        raise ApiError(400, '"pay_rate_usd" must be a number.', "rejected: bad pay_rate_usd") from exc
    if rate is not None and rate < 0:
        raise ApiError(400, '"pay_rate_usd" can\'t be negative.', "rejected: negative pay_rate_usd")
    posted_at, deadline = _when(data, "posted_at"), _when(data, "application_deadline")
    source, _ = Source.objects.get_or_create(kind="bot", url=f"token:{token.pk}", opportunity_type="income",
                                             defaults={"name": token.name[:200], "terms_checked": True})
    external_id = url.split("#")[0].rstrip("/").lower()[:300]
    existing = Opportunity.objects.filter(source=source, external_id=external_id).first()
    if existing:
        return {"id": existing.pk, "created": False}, 200, f"opportunity {existing.pk} already in the inbox"
    opp = Opportunity.objects.create(
        source=source, owner=token.owner, opportunity_type="income", external_id=external_id, url=url, title=title,
        raw_text=_text(data, "text", required=False, limit=20000),
        company=_text(data, "company", required=False, limit=200),
        work_type=work_type, pay_type=pay_type, pay_rate_usd=rate,
        location_requirement=_text(data, "location_requirement", required=False, limit=200) or None,
        skills_required=_string_list(data, "skills_required", 30, 60) or None,
        posted_at=posted_at, application_deadline=deadline, status="inbox", show_anyway=True)
    extract_and_score(opp, use_ai=False)   # safety check + skill fit, no AI cost
    return ({"id": opp.pk, "created": True, "safety_verdict": opp.safety_verdict}, 201,
            f"opportunity {opp.pk}: {title[:80]}")


# ---------------------------------------------------------------------------
# Site suggestions (Admin inbox)
# ---------------------------------------------------------------------------

@bot_endpoint("site:suggest", methods=("POST",))
def site_suggestions_create(request, token):
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


SUGGESTIONS_PAGE_SIZE, SUGGESTIONS_MAX_PAGE_SIZE = 25, 100
SUGGESTION_SORTS = ("created_at", "status_changed_at")


def _missing_scope(scope):
    return ApiError(403, "missing scope", f"rejected: needs {scope}", extra={"required": scope})


def _keys(request, name, choices):
    """A comma-separated filter like status=new,planned - every value must be one of choices."""
    values = [v.strip() for v in (request.GET.get(name) or "").split(",") if v.strip()]
    bad = [v for v in values if v not in dict(choices)]
    if bad:
        raise ApiError(400, f'"{name}" must be one or more of: {", ".join(k for k, _ in choices)}.',
                       f"rejected: bad {name}")
    return values


def _since(request, name):
    """YYYY-MM-DD (from the start of that day) or an ISO date-time; None when not given."""
    raw = (request.GET.get(name) or "").strip()
    if not raw:
        return None
    try:
        moment = parse_datetime(raw)
        day = None if moment else parse_date(raw)
    except ValueError:
        moment = day = None
    if day:
        moment = datetime.combine(day, datetime.min.time())
    if moment is None:
        raise ApiError(400, f'"{name}" must be YYYY-MM-DD or an ISO date-time.', f"rejected: bad {name}")
    return timezone.make_aware(moment) if timezone.is_naive(moment) else moment


def _number(request, name, default, high=None):
    try:
        value = int(request.GET.get(name) or default)
    except ValueError as exc:
        raise ApiError(400, f'"{name}" must be a number.', f"rejected: bad {name}") from exc
    if value < 1 or (high and value > high):
        raise ApiError(400, f'"{name}" must be from 1 to {high}.' if high else f'"{name}" must be 1 or more.',
                       f"rejected: bad {name}")
    return value


@bot_endpoint()
def site_suggestions_list(request, token):
    """suggestions:read sees every bot suggestion; site:suggest alone sees only this token's own."""
    if scope_rules.usable(token, "suggestions:read"):
        qs, own = SiteSuggestion.objects.all(), False
    elif scope_rules.usable(token, "site:suggest"):
        qs, own = SiteSuggestion.objects.filter(token=token), True
    else:
        raise _missing_scope("suggestions:read")
    statuses = _keys(request, "status", SiteSuggestion.STATUSES)
    apps = _keys(request, "app", SiteSuggestion.APPS)
    priorities = _keys(request, "priority", SiteSuggestion.PRIORITIES)
    include = _keys(request, "include", [("description", "")])
    sort = request.GET.get("sort") or "created_at"
    if sort not in SUGGESTION_SORTS:
        raise ApiError(400, '"sort" must be created_at or status_changed_at.', "rejected: bad sort")
    query = (request.GET.get("q") or "").strip()[:200]
    created_since, updated_since = _since(request, "created_since"), _since(request, "updated_since")
    page = _number(request, "page", 1)
    size = _number(request, "page_size", SUGGESTIONS_PAGE_SIZE, SUGGESTIONS_MAX_PAGE_SIZE)
    if statuses:
        qs = qs.filter(status__in=statuses)
    if apps:
        qs = qs.filter(app__in=apps)
    if priorities:
        qs = qs.filter(priority__in=priorities)
    if created_since:
        qs = qs.filter(created_at__gte=created_since)
    if updated_since:
        qs = qs.filter(updated_at__gte=updated_since)
    if query:
        qs = qs.filter(title__icontains=query)
    qs = qs.select_related("token").order_by(F(sort).desc(nulls_last=True), "-pk")
    total = qs.count()
    pages = max(1, -(-total // size))
    rows = []
    for s in qs[(page - 1) * size: page * size]:   # never my_notes
        row = {"id": s.pk, "title": s.title, "app": s.app, "priority": s.priority, "status": s.status,
               "source": s.token.name if s.token else None, "created_at": _iso(s.created_at),
               "status_changed_at": _iso(s.status_changed_at)}
        if "description" in include:
            row["description"] = s.description
        rows.append(row)
    return ({"suggestions": rows, "page": page, "page_size": size, "pages": pages, "total": total,
             "next_page": page + 1 if page < pages else None, "own_only": own}, 200,
            f"listed {len(rows)} site suggestions (page {page}{', own only' if own else ''})")


@bot_endpoint()
def site_suggestion_counts(request, token):
    if not scope_rules.usable(token, "suggestions:read"):
        raise _missing_scope("suggestions:read")
    found = dict(SiteSuggestion.objects.order_by().values("status").annotate(n=Count("pk")).values_list("status", "n"))
    counts = {key: found.get(key, 0) for key, _label in SiteSuggestion.STATUSES}
    return {"counts": counts, "total": sum(counts.values())}, 200, "site suggestion counts"


@csrf_exempt
def site_suggestions(request):
    """One path, two uses: POST adds one (site:suggest), anything else lists (GET only)."""
    return site_suggestions_create(request) if request.method == "POST" else site_suggestions_list(request)


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


# ---------------------------------------------------------------------------
# Activity (albright_labs.activity) - the owner's notifications
# ---------------------------------------------------------------------------

@bot_endpoint("activity:read")
def activity(request, token):
    from albright_labs import activity as labs_activity
    raw = (request.GET.get("since") or "").strip()
    since = timezone.now() - timedelta(days=1)
    if raw:
        parsed = parse_datetime(raw.replace(" ", "+"))
        if parsed is None and parse_date(raw):
            parsed = datetime.combine(parse_date(raw), datetime.min.time())
        if parsed is None:
            raise ApiError(400, '"since" must be an ISO date or datetime, e.g. 2026-11-09 or 2026-11-09T08:00:00Z.',
                           "rejected: bad since")
        since = timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed
    try:
        limit = max(1, min(200, int(request.GET.get("limit") or 50)))
    except ValueError:
        raise ApiError(400, '"limit" must be a number from 1 to 200.', "rejected: bad limit") from None
    owner = token.owner
    events = list(labs_activity.notifications(owner).filter(updated_at__gt=since)[:limit])
    unread = set(labs_activity.unread(owner, labs_activity.visible(owner).filter(pk__in=[e.pk for e in events]))
                 .values_list("pk", flat=True))
    return ({"since": since.isoformat(),
             "events": [{**labs_activity.as_json(e), "unread": e.pk in unread} for e in events]},
            200, f"read {len(events)} activity event(s)")
