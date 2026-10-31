"""What a note can link to (NoteLink): a record in another app - a recipe, a
reseller ledger item or auction lot, an opportunity, a trading strategy.

Every lookup goes through can_open(): the user must be able to open the
record's app (Admin > Users) and, for records that belong to one person (a
ledger item, a strategy), be that person - the same rule the record's own
page applies. So a link can only be added to something its adder can open,
and someone a note is shared with only sees a linked record's name if they
could open it too; otherwise the chip just says which app it's in.

Links come from the picker (search() across the apps the user can open) or
from a pasted site URL (from_url())."""
from dataclasses import dataclass
from urllib.parse import urlsplit

from django.apps import apps as django_apps
from django.urls import Resolver404, resolve, reverse

from albright_labs import app_access, apps_registry

PICKER_LIMIT = 20


@dataclass(frozen=True)
class Kind:
    app: str               # app key (apps_registry)
    type: str              # NoteLink.object_type
    label: str             # "Recipe"
    model: str             # "albright_meals.Recipe"
    title_field: str       # what the chip shows
    url_name: str = ""     # its page on this site (a pasted URL of it becomes this link)
    url_kwarg: str = "pk"
    owner_field: str = ""  # set: only that user may link it (and see its name)
    external_url_field: str = ""   # no page here - the chip opens this (through the link check)

    @property
    def model_class(self):
        return django_apps.get_model(self.model)

    def title(self, obj):
        return str(getattr(obj, self.title_field) or f"{self.label} #{obj.pk}")[:200]

    def url(self, obj):
        if self.url_name:
            return reverse(self.url_name, kwargs={self.url_kwarg: obj.pk})
        return str(getattr(obj, self.external_url_field) or "")[:500]


KINDS = (
    Kind("meals", "recipe", "Recipe", "albright_meals.Recipe", "title", "albright_meals:recipe"),
    Kind("resell", "ledger", "Ledger item", "albright_reselling_app.LedgerEntry", "item",
         "albright_reselling_app:ledger_manage", "entry_id", owner_field="owner"),
    Kind("resell", "lot", "Auction lot", "albright_reselling_app.SourcedLot", "title", external_url_field="url"),
    Kind("opps", "opportunity", "Opportunity", "albright_crypto_app.Opportunity", "title",
         "albright_crypto_app:opportunity"),
    Kind("trade", "strategy", "Strategy", "albright_trading_app.Strategy", "name", "strategy_detail", "strategy_id",
         owner_field="user"),
    Kind("trade", "option_strategy", "Options strategy", "albright_trading_app.OptionStrategy", "name",
         "option_strategy_detail", "strategy_id", owner_field="user"),
)
BY_KEY = {(k.app, k.type): k for k in KINDS}
BY_URL_NAME = {k.url_name: k for k in KINDS if k.url_name}


def _allowed_apps(user):
    from albright_labs.models import UserPreference
    return set(app_access.allowed_keys(user, UserPreference.for_user(user)))


def kinds_for(user, allowed=None):
    allowed = _allowed_apps(user) if allowed is None else allowed
    return [k for k in KINDS if k.app in allowed]


def _queryset(kind, user):
    qs = kind.model_class.objects.all()
    return qs.filter(**{kind.owner_field: user}) if kind.owner_field else qs


def get_record(user, app, object_type, object_id, allowed=None):
    """(kind, record) if this user can open it, else None."""
    kind = BY_KEY.get((app, object_type))
    if kind is None or kind not in kinds_for(user, allowed):
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


def search(user, query, limit=PICKER_LIMIT):
    """Records matching `query` in the apps this user can open (the picker)."""
    query = (query or "").strip()[:100]
    if len(query) < 2:
        return []
    results = []
    for kind in kinds_for(user):
        qs = _queryset(kind, user).filter(**{f"{kind.title_field}__icontains": query}).order_by("-pk")[:limit]
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
