"""Programs, made easy (/fitness/programs/...): the exercise type-ahead (search the library, or make my own in one
tap) used by the guided setup. Everything is the owner's own - anyone else's program or exercise is a 404."""
from django.contrib.auth.decorators import login_required
from django.db.models import Case, IntegerField, Value, When
from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from .models import EQUIPMENT, Exercise

SEARCH_LIMIT = 12
KIND_EQUIPMENT = {Exercise.CARDIO: "cardio_machine", Exercise.BODYWEIGHT: "bodyweight", Exercise.MOBILITY: "none"}


def search_exercises(user, q, limit=SEARCH_LIMIT):
    """Library + my own exercises whose name has every word, names that start with the text first."""
    q = " ".join((q or "").split())[:60]
    if not q:
        return []
    qs = Exercise.objects.visible_to(user)
    for word in q.split():
        qs = qs.filter(name__icontains=word)
    qs = qs.annotate(rank=Case(When(name__iexact=q, then=Value(0)), When(name__istartswith=q, then=Value(1)),
                               default=Value(2), output_field=IntegerField()))
    return list(qs.order_by("rank", "name")[:limit])


def exercise_json(e):
    return {"id": e.pk, "name": e.name, "kind": e.kind, "kind_label": e.get_kind_display(), "mine": e.owner_id is not None}


def create_exercise(user, name, kind=Exercise.STRENGTH):
    """My own exercise by that name - or the one I can already see by that name (any case). -> (exercise, created)"""
    name = " ".join((name or "").split())[:100]
    if not name:
        return None, False
    existing = Exercise.objects.visible_to(user).filter(name__iexact=name).first()
    if existing:
        return existing, False
    kind = kind if kind in dict(Exercise.KINDS) else Exercise.STRENGTH
    equipment = KIND_EQUIPMENT.get(kind, "other")
    return Exercise.objects.create(owner=user, name=name, kind=kind,
                                   equipment=equipment if equipment in dict(EQUIPMENT) else "other"), True


@login_required
@never_cache
def exercise_search(request):
    """GET ?q= -> {"results": [...], "create": the typed name when nothing matches it exactly (or null)}."""
    q = " ".join(request.GET.get("q", "").split())[:60]
    found = search_exercises(request.user, q)
    exact = any(e.name.lower() == q.lower() for e in found)
    return JsonResponse({"results": [exercise_json(e) for e in found], "create": q if q and not exact else None})


@login_required
@require_POST
def exercise_create(request):
    """POST name (+ kind) -> {"exercise": {...}, "created": bool}. Making one that already exists returns it."""
    ex, created = create_exercise(request.user, request.POST.get("name"), request.POST.get("kind", Exercise.STRENGTH))
    if ex is None:
        return JsonResponse({"error": "Give the exercise a name."}, status=400)
    return JsonResponse({"exercise": exercise_json(ex), "created": created}, status=201 if created else 200)
