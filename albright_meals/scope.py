"""Who sees what in Meals - every page, the Bot API, the Planner's meal overlay and Notes' recipe links go through
here.

  - Everything (recipes, the plan, the grocery list, ideas, settings) belongs to one household; only its members see
    or change it. Anything else is a 404.
  - A recipe can also be read - never changed - by every Meals user when it's in the "Recipe library", and by one
    person at a time it's shared with (RecipeShare). They can save a copy into their own household.
  - Ratings are people's own stamps, shown only within their household: on any recipe (ours, shared, library) you
    see your household's stamps and nobody else's.
  - Someone with Meals access but no household gets the "Set up your kitchen" page (kitchen_required).
"""
from functools import wraps

from django.db.models import Count, Exists, OuterRef, Q
from django.http import Http404
from django.shortcuts import render

from albright_labs.households import member_ids
from albright_labs.models import household_of

from .models import Recipe, RecipeRating, RecipeShare

HIGH = (RecipeRating.FAVORITE, RecipeRating.STAPLE)


def kitchen_required(view):
    """The view runs with request.household (the user's); without one, the "Set up your kitchen" page."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        request.household = household_of(request.user)
        if request.household is None:
            return render(request, "albright_meals/setup.html", status=200)
        return view(request, *args, **kwargs)
    return wrapped


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------

def own(household):
    return Recipe.objects.filter(household=household)


def shared_with(user, household=None):
    """Recipes other households shared with this person."""
    qs = Recipe.objects.filter(shares__shared_with=user)
    return qs.exclude(household=household) if household else qs


def library():
    return Recipe.objects.filter(visibility=Recipe.LIBRARY)


def readable(user, household):
    """Every recipe this person may read: their household's, the library, and ones shared with them."""
    shared = RecipeShare.objects.filter(recipe=OuterRef("pk"), shared_with=user)
    return Recipe.objects.filter(Q(household=household) | Q(visibility=Recipe.LIBRARY) | Exists(shared))


def readable_recipe(user, household, pk):
    """(recipe, editable) - or Http404 when it isn't theirs, in the library or shared with them."""
    recipe = readable(user, household).select_related("created_by", "household").filter(pk=pk).first()
    if recipe is None:
        raise Http404
    return recipe, recipe.household_id == getattr(household, "pk", None)


def own_recipe(household, pk, queryset=None):
    """One of the household's recipes (to change) - anyone else's is a 404, even one they can read."""
    recipe = (queryset if queryset is not None else Recipe.objects).filter(household=household, pk=pk).first()
    if recipe is None:
        raise Http404
    return recipe


# ---------------------------------------------------------------------------
# Ratings - private to the household
# ---------------------------------------------------------------------------

def stamps(recipes, household):
    """{recipe pk: [ratings by members of this household]} - never anyone else's."""
    ids = member_ids(household)
    out = {}
    for r in RecipeRating.objects.filter(recipe__in=list(recipes), user_id__in=ids).select_related("user").order_by("user_id"):
        out.setdefault(r.recipe_id, []).append(r)
    return out


def parse_filters(params, user, household):
    """The rating filters from a query string: {"rated_by": "me" | "anyone" | <member id>, "levels": [1-6],
    "none": bool, "love": bool} - anything else ignored. rated_by is only kept if it's someone in the household."""
    ids = member_ids(household)
    raw = (params.get("rated_by") or "me").strip()
    if raw == "anyone":
        rated_by = "anyone"
    elif raw == "me" or not raw.isdigit() or int(raw) not in ids:
        rated_by = "me"
    else:
        rated_by = "me" if int(raw) == user.pk else int(raw)
    values = params.getlist("rating") if hasattr(params, "getlist") else list(params.get("rating") or [])
    if isinstance(values, str):
        values = [values]
    levels = sorted({int(v) for v in values if str(v).isdigit() and int(v) in dict(RecipeRating.LEVELS)})
    return {"rated_by": rated_by, "levels": levels, "none": "none" in values,
            "love": str(params.get("love") or "") in ("1", "true")}


def apply_filters(qs, filters, user, household):
    """Recipes matching the rating filters - judged only by this household's stamps.
      rated_by me / a member: their stamp is one of `levels` (or they haven't rated it, with "none");
      rated_by anyone: someone in the household's stamp is one of `levels` (or nobody here rated it);
      love ("We both love"): at least two of the household rated it and every one of them Favorite or Staple."""
    ids = member_ids(household)
    who = ids if filters["rated_by"] == "anyone" else {user.pk if filters["rated_by"] == "me" else filters["rated_by"]}
    if filters["levels"] or filters["none"]:
        rated = RecipeRating.objects.filter(recipe=OuterRef("pk"), user_id__in=who)
        condition = Q()
        if filters["levels"]:
            condition |= Q(Exists(rated.filter(level__in=filters["levels"])))
        if filters["none"]:
            condition |= ~Q(Exists(rated))
        qs = qs.filter(condition)
    if filters["love"]:
        low = RecipeRating.objects.filter(recipe=OuterRef("pk"), user_id__in=ids).exclude(level__in=HIGH)
        qs = qs.annotate(_raters=Count("ratings", filter=Q(ratings__user_id__in=ids), distinct=True)) \
            .filter(_raters__gte=2).exclude(Exists(low))
    return qs


def filters_active(filters):
    return bool(filters["levels"] or filters["none"] or filters["love"])
