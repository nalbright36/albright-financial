"""A Visual ready to draw: its queries re-run now (so it's always current) against the OWNER's data, the rows put in
its spec with the app colors (spec.inject). Who may open one: its owner; a saved one also everyone it's shared with
(albright_labs.sharing - they see the owner's numbers for this chart only because the owner shared it)."""
from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from . import metrics, spec


def max_saved():
    return getattr(settings, "VISUALS_MAX_SAVED", 20)


def can_view(user, visual):
    if not (user and user.is_authenticated):
        return False
    if visual.owner_id == user.pk:
        return True
    if not visual.saved:
        return False
    from albright_labs import sharing
    return sharing.can_view(user, visual)


def saved_target(visual):
    """The saved Visual this draft (or an earlier version of it in the chat) was saved as - what "Replace" overwrites."""
    seen, current = 0, visual
    while current is not None and seen < 30:
        if current.saved_as_id and current.saved_as and current.saved_as.saved:
            return current.saved_as
        current, seen = current.previous, seen + 1
    return None


def card(visual, viewer):
    """What the chat (and the pages) need before drawing: the card's text and where to fetch the chart."""
    mine = visual.owner_id == viewer.pk
    out = {"id": visual.pk, "title": visual.title, "summary": visual.summary, "version": visual.version,
           "saved": visual.saved, "data_url": reverse("assistant:visual_data", args=[visual.pk]),
           "mine": mine}
    if visual.saved:
        out["page_url"] = reverse("assistant:visual", args=[visual.pk])
        if not mine:
            from albright_labs.sharing import person_name
            out["shared_by"] = person_name(visual.owner)
    elif mine:
        out["save_url"] = reverse("assistant:visual_save", args=[visual.pk])
        target = saved_target(visual)
        out["replaces"] = {"id": target.pk, "title": target.title} if target else None
    return out


def payload(visual, viewer):
    """card() plus the spec with today's rows - or "error" if a query can't run now (e.g. lost access to an app)."""
    out = card(visual, viewer)
    try:
        datasets = {name: metrics.series(visual.owner, query, owner=visual.owner)
                    for name, query in (visual.queries or {}).items()}
    except metrics.MetricError as exc:
        return {**out, "error": str(exc)}
    keys = [k for q in (visual.queries or {}).values() for k in q.get("metrics", [])]
    out["spec"] = spec.inject(visual.spec, datasets, metrics.colors(visual.owner, keys))
    out["rows"] = sum(len(r) for r in datasets.values())
    out["updated"] = timezone.now().isoformat()
    return out
