"""One search source for the whole site: the global search (Ctrl/Cmd K - albright_labs.search_views, the Bot API's
searchEverything) and what a note can link to (NoteLink - a recipe, a Planner task, a transaction, an idea...).

Every lookup goes through a Kind's scope - the same rule the record's own page applies: the user must be able to
open the record's app (Admin > Users; never an archived app), and the record must be theirs, in a space they see
(Meals, Money - albright_labs.circles), or shared with them (a person or a circle - albright_labs.sharing). So a link
can only be added to something its adder can open, someone a note is shared with only sees a linked record's name if
they could open it too, and search never shows anything its user couldn't open.

find() is the search: every word of the query (any order, case-insensitive) must appear in a Kind's title or body
fields; title matches rank above body matches, then the most recent. search() is the note link picker on top of it
(linkable Kinds only); a pasted site URL of a record becomes a link through from_url()."""
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.parse import urlencode, urlsplit
import re

from django.apps import apps as django_apps
from django.conf import settings
from django.db.models import Case, F, IntegerField, Q, TextField, Value, When
from django.db.models.functions import Cast
from django.urls import NoReverseMatch, Resolver404, resolve, reverse

from albright_labs import app_access, apps_registry

PICKER_LIMIT = 20
PER_GROUP = 5            # global search: results per group (All)
PER_GROUP_FILTERED = 30  # ...and with one group picked (app=)
MAX_WORDS = 6
QUERY_MAX = 100


@dataclass(frozen=True)
class Kind:
    app: str               # app key (apps_registry); "" = not an app (People - always there)
    type: str              # NoteLink.object_type
    label: str             # "Recipe"
    model: str             # "albright_meals.Recipe"
    title_field: str       # what the chip / result shows
    url_name: str = ""     # its page on this site (a pasted URL of it becomes this link)
    url_kwarg: str = "pk"
    owner_field: str = ""  # set: only that user may link it (and see its name)
    external_url_field: str = ""   # no page here - the chip opens this (through the link check)
    # Global search ("" = only the note link picker - the archived apps' records).
    group: str = ""                 # GROUPS key
    title_also: tuple = ()          # more fields that count as the title (People's names)
    body_fields: tuple = ()         # also searched, ranked below a title match (JSON fields are searched as text)
    recent: str = "-pk"             # newest first among equal matches
    icon: str = ""                  # labs/search.js ICONS
    linkable: bool = True           # the note link picker offers it
    scope: Callable = None          # user -> everything of this kind they may open (else owner_field / all)
    info: Callable = None           # (user, [obj]) -> {pk: "Thu, Nov 27 · Shared with Home"}
    href: Callable = None           # obj -> its page (else url_name)
    show: Callable = None           # obj -> its title (else title_field)
    match: Callable = None          # word -> a Q it also matches (Money: an amount)
    related: tuple = ()             # select_related

    @property
    def model_class(self):
        return django_apps.get_model(self.model)

    def title(self, obj):
        if self.show:
            return str(self.show(obj))[:200]
        return str(getattr(obj, self.title_field) or f"{self.label} #{obj.pk}")[:200]

    def url(self, obj):
        if self.href:
            return self.href(obj)
        if self.url_name:
            return reverse(self.url_name, kwargs={self.url_kwarg: obj.pk})
        return str(getattr(obj, self.external_url_field) or "")[:500]


# ---------------------------------------------------------------------------
# What each Kind may show: scopes, info lines
# ---------------------------------------------------------------------------

def _day(d):
    return f"{d:%a}, {d:%b} {d.day}" if d else ""


def _short_day(d):
    return f"{d:%b} {d.day}" if d else ""


def _join(*parts):
    return " · ".join(p for p in parts if p)


def _first_name(user):
    return ((user.first_name or user.get_username()).strip()) if user else ""


def _share_lines(user, objs, owner_attr="owner"):
    """{pk: "Shared with Home" (mine, shared) / "From Kim" (someone else's)} for sharing Kinds."""
    from django.contrib.contenttypes.models import ContentType
    from albright_labs.models import ShareGrant
    if not objs:
        return {}
    mine = [o.pk for o in objs if getattr(o, f"{owner_attr}_id") == user.pk]
    names = {}
    if mine:
        grants = ShareGrant.objects.filter(content_type=ContentType.objects.get_for_model(type(objs[0])),
                                           object_id__in=mine, hidden_at__isnull=True).select_related("circle", "user")
        for g in grants:
            names.setdefault(g.object_id, []).append(g.circle.name if g.circle_id else _first_name(g.user))
    out = {}
    for o in objs:
        if getattr(o, f"{owner_attr}_id") != user.pk:
            out[o.pk] = f"From {_first_name(getattr(o, owner_attr))}"
        elif names.get(o.pk):
            who = names[o.pk]
            out[o.pk] = f"Shared with {who[0]}" + (f" +{len(who) - 1}" if len(who) > 1 else "")
    return out


def _planner_scope(user):
    from albright_planner.models import ScheduleItem
    return ScheduleItem.objects.visible_to(user).filter(archived=False)


def _planner_info(user, objs):
    shares = _share_lines(user, objs)
    out = {}
    for o in objs:
        when = _day(o.next_due or o.date)
        if o.start_time:
            when += f" {o.start_time:%I:%M %p}".replace(" 0", " ").lower()
        out[o.pk] = _join(when, "Repeats" if o.recurrence else "", shares.get(o.pk, ""))
    return out


def _checklist_scope(user):
    from albright_planner.models import TaskChecklistItem
    return TaskChecklistItem.objects.filter(task__in=_planner_scope(user))


def _checklist_info(user, objs):
    return {o.pk: f"Checklist item in “{o.task.title}”" for o in objs}


def _note_scope(user):
    from albright_notes.models import Note
    return Note.objects.visible_to(user)


def _note_info(user, objs):
    shares = _share_lines(user, objs)
    return {o.pk: _join(f"Edited {_short_day(o.updated_at.date())}", "Archived" if o.archived else "",
                        shares.get(o.pk, "")) for o in objs}


def _recipe_scope(user):
    from albright_meals import scope
    return scope.readable(user)


def _recipe_info(user, objs):
    from albright_labs import circles
    mine = circles.space_ids(user, "meals")
    out = {}
    for o in objs:
        minutes = o.total_time_minutes or ((o.prep_minutes or 0) + (o.cook_minutes or 0))
        where = "" if o.space_id in mine else ("Shared library" if o.visibility == "library" else "Shared with you")
        out[o.pk] = _join("Recipe", f"{minutes} min" if minutes else "", where)
    return out


def _money_scope(user):
    from albright_money.models import Transaction
    return Transaction.objects.visible_to(user)


def _money(amount):
    sign = "−" if amount < 0 else "+"
    return f"{sign}${abs(amount):,.2f}"


def _money_info(user, objs):
    out = {}
    for o in objs:
        category = "Transfer" if o.is_transfer else (o.category.name if o.category_id else "")
        out[o.pk] = _join(_short_day(o.date), _money(o.amount), category, o.account.name)
    return out


def _amount_q(word):
    """"212.40", "$212", "-212.40" also match a transaction of that amount (in or out)."""
    cleaned = word.replace("$", "").replace(",", "").replace("−", "").lstrip("+-")
    if not re.fullmatch(r"\d+(\.\d{1,2})?", cleaned):
        return None
    try:
        amount = Decimal(cleaned)
    except InvalidOperation:
        return None
    return Q(amount=amount) | Q(amount=-amount)


def _idea_info(user, objs):
    return {o.pk: _join("Idea", o.get_status_display()) for o in objs}


def _workout_scope(user):
    from albright_fitness.models import Workout
    return Workout.objects.visible_to(user)


def _workout_title(o):
    return o.name or (o.program_day.name if o.program_day_id else "") or o.program_day_name or "Workout"


def _workout_info(user, objs):
    shares = _share_lines(user, objs)
    out = {}
    for o in objs:
        program = o.program_day.program.name if o.program_day_id else o.program_name
        out[o.pk] = _join(_day(o.date), "Skipped" if o.status == "skipped" else "", program, shares.get(o.pk, ""))
    return out


def _program_scope(user):
    from albright_fitness.models import Program
    from albright_labs import sharing
    return Program.objects.filter(Q(owner=user) | Q(pk__in=sharing.shared_ids(user, Program))
                                  | Q(members__user=user)).distinct()


def _program_info(user, objs):
    shares = _share_lines(user, objs)
    out = {}
    for o in objs:
        state = "Archived" if o.archived_at else ("Draft" if o.is_draft else ("Active" if o.active else "Paused"))
        out[o.pk] = _join("Program", f"{o.weeks} weeks" if o.weeks else "", state, shares.get(o.pk, ""))
    return out


def _my_circles(user):
    from albright_labs.models import CircleMembership
    return CircleMembership.objects.filter(user=user, status=CircleMembership.ACTIVE).values("circle_id")


def _people_scope(user):
    """People in my circles (active members, both of us) - never anyone else."""
    from django.contrib.auth import get_user_model
    from albright_labs.models import CircleMembership
    ids = CircleMembership.objects.filter(circle_id__in=_my_circles(user), status=CircleMembership.ACTIVE) \
        .exclude(user=user).values("user_id")
    return get_user_model().objects.filter(is_active=True, pk__in=ids)


def _person_title(o):
    return o.get_full_name().strip() or o.get_username()


def _people_circles(user, objs):
    from albright_labs.models import CircleMembership
    rows = CircleMembership.objects.filter(circle_id__in=_my_circles(user), user__in=objs,
                                           status=CircleMembership.ACTIVE).select_related("circle") \
        .order_by("circle__name")
    out = {}
    for m in rows:
        out.setdefault(m.user_id, []).append(m.circle)
    return out


def _people_info(user, objs):
    circles = _people_circles(user, objs)
    return {o.pk: _join(o.get_username() if o.get_full_name().strip() else "",
                        "In " + ", ".join(c.name for c in circles.get(o.pk, []))) for o in objs}


def _person_href(user):
    def href(o):
        circles = _people_circles(user, [o]).get(o.pk)
        return reverse("labs:circle", args=[circles[0].pk]) if circles else reverse("labs:people")
    return href


KINDS = (
    Kind("planner", "task", "Task", "albright_planner.ScheduleItem", "title", "albright_planner:item",
         group="planner", body_fields=("notes",), recent="-updated_at", icon="cal", scope=_planner_scope,
         info=_planner_info, related=("owner",)),
    Kind("planner", "checklist", "Checklist item", "albright_planner.TaskChecklistItem", "text",
         group="planner", recent="-task__updated_at", icon="check", linkable=False, scope=_checklist_scope,
         info=_checklist_info, href=lambda o: reverse("albright_planner:item", args=[o.task_id]), related=("task",)),
    Kind("notes", "note", "Note", "albright_notes.Note", "title", "albright_notes:note",
         group="notes", body_fields=("body", "tags"), recent="-updated_at", icon="note", linkable=False,
         scope=_note_scope, info=_note_info, related=("owner",)),
    Kind("meals", "recipe", "Recipe", "albright_meals.Recipe", "title", "albright_meals:recipe",
         group="recipes", body_fields=("description", "ingredients", "tags", "notes"), recent="-updated_at",
         icon="fork", scope=_recipe_scope, info=_recipe_info),
    Kind("money", "transaction", "Transaction", "albright_money.Transaction", "payee", "albright_money:transaction",
         group="money", body_fields=("notes", "tags"), recent="-date", icon="card", scope=_money_scope,
         info=_money_info, match=_amount_q, related=("account", "category"),
         show=lambda o: o.payee or ("Transfer" if o.is_transfer else "Transaction")),
    Kind("admin", "idea", "Idea", "albright_control.Idea", "title", "albright_control:idea",
         group="ideas", body_fields=("description", "my_notes"), recent="-updated_at", icon="bulb", info=_idea_info),
    Kind("fitness", "workout", "Workout", "albright_fitness.Workout", "name", "albright_fitness:summary",
         group="fitness", title_also=("program_day__name", "program_day_name"), body_fields=("notes",),
         recent="-date", icon="dumb", scope=_workout_scope, info=_workout_info, show=_workout_title,
         related=("owner", "program_day__program")),
    Kind("fitness", "program", "Program", "albright_fitness.Program", "name", "albright_fitness:program",
         group="fitness", recent="-created_at", icon="dumb", scope=_program_scope, info=_program_info,
         related=("owner",)),
    Kind("", "person", "Person", settings.AUTH_USER_MODEL, "username", group="people",
         title_also=("first_name", "last_name"), recent="username", icon="user", linkable=False,
         scope=_people_scope, info=_people_info, show=_person_title),
    # Archived apps: kept so existing links still show; search skips them (app_access never allows an archived app).
    Kind("resell", "ledger", "Ledger item", "albright_reselling_app.LedgerEntry", "item",
         "albright_reselling_app:ledger_manage", "entry_id", owner_field="owner"),
    Kind("resell", "lot", "Auction lot", "albright_reselling_app.SourcedLot", "title", external_url_field="url"),
    Kind("opps", "opportunity", "Opportunity", "albright_crypto_app.Opportunity", "title",
         "albright_crypto_app:opportunity", owner_field="owner"),
    Kind("trade", "strategy", "Strategy", "albright_trading_app.Strategy", "name", "strategy_detail", "strategy_id",
         owner_field="user"),
    Kind("trade", "option_strategy", "Options strategy", "albright_trading_app.OptionStrategy", "name",
         "option_strategy_detail", "strategy_id", owner_field="user"),
)
BY_KEY = {(k.app, k.type): k for k in KINDS}
BY_URL_NAME = {k.url_name: k for k in KINDS if k.url_name and k.linkable}


def _see_all(url_name, **extra):
    def url(q):
        return reverse(url_name) + "?" + urlencode({**extra, "q": q})
    return url


@dataclass(frozen=True)
class Group:
    key: str
    label: str
    accent: str            # the color dot (style.css --app-<accent>)
    see_all: Callable = None   # q -> that app's list filtered by it (None: the palette shows more itself)


GROUPS = (
    Group("planner", "Planner", "planner"),
    Group("notes", "Notes", "notes", _see_all("albright_notes:list")),
    Group("recipes", "Recipes", "meals", _see_all("albright_meals:recipes")),
    Group("money", "Money", "money", _see_all("albright_money:transactions")),
    Group("ideas", "Ideas", "opportunities", _see_all("albright_control:ideas", status="all")),
    Group("fitness", "Fitness", "fitness"),
    Group("people", "People", "labs"),
)
GROUP_BY_KEY = {g.key: g for g in GROUPS}


def _allowed_apps(user):
    from albright_labs.models import UserPreference
    return set(app_access.allowed_keys(user, UserPreference.for_user(user)))


def kinds_for(user, allowed=None, linkable=False):
    """The Kinds this user can open (their apps; People always) - only the note picker's with linkable."""
    allowed = _allowed_apps(user) if allowed is None else allowed
    return [k for k in KINDS if (k.app in allowed or (not k.app and k.group)) and (k.linkable or not linkable)]


def groups_for(user, allowed=None):
    """[Group] this user has anything to search in (the palette's filter chips)."""
    have = {k.group for k in kinds_for(user, allowed) if k.group}
    return [g for g in GROUPS if g.key in have]


def _queryset(kind, user):
    if kind.scope:
        return kind.scope(user)
    qs = kind.model_class.objects.all()
    return qs.filter(**{kind.owner_field: user}) if kind.owner_field else qs


def get_record(user, app, object_type, object_id, allowed=None):
    """(kind, record) if this user can open it and a note can link it, else None."""
    kind = BY_KEY.get((app, object_type))
    if kind is None or kind not in kinds_for(user, allowed, linkable=True):
        return None
    try:
        object_id = int(object_id)
    except (TypeError, ValueError):
        return None
    record = _queryset(kind, user).filter(pk=object_id).first()
    return (kind, record) if record else None


def from_url(user, url, host=""):
    """A pasted link to a record's page on this site -> (kind, record), or None (not a record page, another
    site, or something this user can't open)."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in ("", "http", "https") or (parts.netloc and parts.netloc.lower() != (host or "").lower()):
        return None
    try:
        match = resolve(parts.path or "/")
    except Resolver404:
        return None
    kind = BY_URL_NAME.get(match.view_name)
    if kind is None:
        return None
    return get_record(user, kind.app, kind.type, match.kwargs.get(kind.url_kwarg))


# ---------------------------------------------------------------------------
# The search
# ---------------------------------------------------------------------------

def words_of(query):
    query = " ".join((query or "").split())[:QUERY_MAX]
    return [w for w in query.split(" ") if w][:MAX_WORDS]


def _is_json(model, path):
    if "__" in path:
        return False
    try:
        return model._meta.get_field(path).get_internal_type() == "JSONField"
    except Exception:
        return False


def _matching(kind, qs, words):
    """qs narrowed to rows with every word somewhere in the title or body, annotated _title_hit (1: every word is
    in the title)."""
    model = kind.model_class
    title_fields = (kind.title_field,) + kind.title_also
    body = []
    for name in kind.body_fields:
        if _is_json(model, name):
            alias = f"_text_{name}"
            qs = qs.annotate(**{alias: Cast(name, TextField())})
            body.append(alias)
        else:
            body.append(name)
    title_all, every = Q(), Q()
    for word in words:
        in_title = Q()
        for name in title_fields:
            in_title |= Q(**{f"{name}__icontains": word})
        anywhere = in_title
        for name in body:
            anywhere |= Q(**{f"{name}__icontains": word})
        extra = kind.match(word) if kind.match else None
        if extra is not None:
            anywhere |= extra
            in_title |= extra
        title_all &= in_title
        every &= anywhere
    return qs.filter(every).annotate(_title_hit=Case(When(title_all, then=Value(1)), default=Value(0),
                                                     output_field=IntegerField()))


def _order(kind):
    field = kind.recent.lstrip("-")
    recent = F(field).desc(nulls_last=True) if kind.recent.startswith("-") else F(field).asc()
    return ("-_title_hit", recent, "-pk")


def _plain(value):
    if isinstance(value, (list, tuple)):
        return " ".join(_plain(v) for v in value)
    if isinstance(value, dict):
        return str(value.get("original") or value.get("name") or "")
    return str(value or "")


BULLET = re.compile(r"(?m)^\s*(?:[-+*]|\d+[.)])\s+")
MARKDOWN = re.compile(r"(\[[ xX]\]|[#*_>`~|]+|!?\[([^\]]*)\]\([^)]*\))")


def snippet(text, words, width=90):
    """A short piece of text around the first match: "…call the tile guy Thursday…" (plain - the page marks the
    words)."""
    text = BULLET.sub("", text or "")
    text = " ".join(MARKDOWN.sub(lambda m: m.group(2) or " ", text).split())
    if not text:
        return ""
    lower = text.lower()
    hits = [lower.find(w.lower()) for w in words if w.lower() in lower]
    if not hits:
        return ""
    at = min(hits)
    start = max(0, at - 30)
    if start:
        space = text.rfind(" ", 0, start + 1)
        start = space + 1 if space > at - 45 else start
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")


def _hit(kind, obj, words, info):
    group = GROUP_BY_KEY.get(kind.group)
    body = ""
    if not obj._title_hit and kind.body_fields:
        body = snippet(" ".join(_plain(getattr(obj, name, "")) for name in kind.body_fields), words)
    try:
        url = kind.url(obj)
    except NoReverseMatch:
        url = ""
    return {"group": kind.group, "app": kind.app, "type": kind.type, "id": obj.pk, "title": kind.title(obj),
            "snippet": body, "info": info.get(obj.pk, ""), "url": url, "icon": kind.icon, "kind": kind.label,
            "accent": group.accent if group else "", "title_match": bool(obj._title_hit)}


def find_kind(user, kind, words, limit):
    """Up to `limit` hits of one Kind (title matches first, then the most recent) and whether there are more."""
    qs = _queryset(kind, user)
    if kind.related:
        qs = qs.select_related(*kind.related)
    rows = list(_matching(kind, qs, words).order_by(*_order(kind))[:limit + 1])
    if kind.type == "person":
        href = _person_href(user)
        kind = replace(kind, href=href)
    info = kind.info(user, rows[:limit]) if kind.info else {}
    return [_hit(kind, obj, words, info) for obj in rows[:limit]], len(rows) > limit


def find(user, query, group=None, limit=None, only=None):
    """The global search: [{"key", "label", "accent", "see_all", "more", "results": [...]}] - one entry per group with
    results, in GROUPS order. group: only that one (more results each). only: the groups that may be searched at all
    (a Bot API key's scopes)."""
    words = words_of(query)
    if not words or len(" ".join(words)) < 2:
        return []
    limit = limit or (PER_GROUP_FILTERED if group else PER_GROUP)
    allowed = _allowed_apps(user)
    by_group = {}
    for kind in kinds_for(user, allowed):
        if not kind.group or (group and kind.group != group) or (only is not None and kind.group not in only):
            continue
        hits, more = find_kind(user, kind, words, limit)
        entry = by_group.setdefault(kind.group, {"results": [], "more": False})
        entry["results"] += hits
        entry["more"] = entry["more"] or more
    out = []
    q = " ".join(words)
    for g in GROUPS:
        entry = by_group.get(g.key)
        if not entry or not entry["results"]:
            continue
        results = sorted(entry["results"], key=lambda r: not r["title_match"])   # stable: each Kind's order kept
        more = entry["more"] or len(results) > limit
        out.append({"key": g.key, "label": g.label, "accent": g.accent, "results": results[:limit], "more": more,
                    "see_all": g.see_all(q) if g.see_all else ""})
    return out


def search(user, query, limit=PICKER_LIMIT):
    """The note link picker: records matching `query` that a note can link, in the apps this user can open."""
    words = words_of(query)
    if len(" ".join(words)) < 2:
        return []
    results = []
    for kind in kinds_for(user, linkable=True):
        if kind.group:
            hits, _more = find_kind(user, kind, words, limit)
            results += [{"app": kind.app, "type": kind.type, "id": h["id"], "label": h["title"], "kind": kind.label,
                         "app_label": apps_registry.BY_KEY[kind.app].label,
                         "accent": apps_registry.BY_KEY[kind.app].accent} for h in hits]
        else:
            qs = _matching(kind, _queryset(kind, user), words).order_by("-_title_hit", "-pk")[:limit]
            results += [option(kind, obj) for obj in qs]
    return results[:limit]


def option(kind, obj):
    app = apps_registry.BY_KEY[kind.app]
    return {"app": kind.app, "type": kind.type, "id": obj.pk, "label": kind.title(obj), "kind": kind.label,
            "app_label": app.label, "accent": app.accent}


def add(note, kind, record):
    """Link the note to this record (once); returns the NoteLink."""
    from .models import NoteLink
    link, _created = NoteLink.objects.get_or_create(
        note=note, app=kind.app, object_type=kind.type, object_id=record.pk,
        defaults={"label": kind.title(record), "url": kind.url(record)})
    return link


def chips(user, links, allowed=None):
    """How each NoteLink shows to this user: [{id, label, href, kind, app_label, accent, open}] - a record they
    can't open (or that's gone) shows only its app, never its name or link."""
    from .render import outbound_href
    allowed = _allowed_apps(user) if allowed is None else allowed
    wanted = {}
    for link in links:
        kind = BY_KEY.get((link.app, link.object_type))
        if kind and kind.app in allowed:
            wanted.setdefault(kind, set()).add(link.object_id)
    openable = {kind: set(_queryset(kind, user).filter(pk__in=ids).values_list("pk", flat=True))
                for kind, ids in wanted.items()}
    out = []
    for link in links:
        kind = BY_KEY.get((link.app, link.object_type))
        app = apps_registry.BY_KEY.get(link.app)
        can = bool(kind and link.object_id in openable.get(kind, ()))
        href = ""
        if can:
            href = outbound_href(link.url) if kind.external_url_field else link.url
        out.append({"id": link.pk, "label": link.label if can else f"{app.label if app else 'Other'} record",
                    "href": href, "kind": kind.label if kind else "", "app_label": app.label if app else "",
                    "accent": app.accent if app else "", "open": can})
    return out
