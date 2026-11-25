"""Who sees what in Meals - every page, the Bot API, the Planner's meal overlay and Notes' recipe links go through
here, and here asks albright_labs.circles (the one place membership is checked).

  - Everything (recipes, the plan, the grocery list, ideas, settings) belongs to one space: someone's Personal space or
    a circle's. A circle's space shows only to its active members who opted in to Meals; a Personal space only to its
    person. Anything else is a 404.
  - Each page shows one space at a time - the switcher in the Meals header (space_required: ?space=<id>, remembered
    per person), Personal by default.
  - A recipe can also be read - never changed - by every Meals user when it's in the "Recipe library", and by one
    person or circle it's shared with (albright_labs.sharing - view, or edit its content). They can save a copy into
    their own space; moving a recipe into another space is the owner's "Move to space".
  - Ratings are people's own stamps, shown only to the people who see the recipe's space.
"""
from functools import wraps

from django.db.models import Count, Exists, OuterRef, Q
from django.http import Http404

from albright_labs import circles, sharing

from .models import Recipe, RecipeRating

APP = "meals"
HIGH = (RecipeRating.FAVORITE, RecipeRating.STAPLE)


def space_required(view):
    """The view runs with request.space (the one being shown: ?space=<id> when the user may see it, else the one
    they last picked, else Personal) and request.spaces (every space they see in Meals - the switcher)."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        request.spaces = circles.visible_spaces(request.user, APP)
        if not request.spaces:
            raise Http404
        request.space = circles.current_space(request.user, APP, request.GET.get("space") or request.POST.get("space"))
        return view(request, *args, **kwargs)
    return wrapped



def space_ids(user):
    return circles.space_ids(user, APP)


def member_ids(space):
    """Who sees this space in Meals - whose stamps count."""
    return set(circles.space_members(space, APP).values_list("pk", flat=True))


def title(space):
    """How a space is credited: a circle's name, or "<first name>'s recipes" for someone's Personal space."""
    if space.circle_id:
        return space.circle.name
    person = space.user
    return f"{(person.first_name or person.get_username()).strip()}'s recipes"


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------

def own(space):
    return Recipe.objects.filter(space=space)


def shared_with(user, space=None):
    """Recipes shared with this person (directly or through a circle) from spaces they don't see."""
    return sharing.shared_with_queryset(user, Recipe).exclude(space_id__in=space_ids(user))


def library():
    return Recipe.objects.filter(visibility=Recipe.LIBRARY)


# Recipes list source ("Showing: All recipes"): ?from=all|mine|library|shared|space-<id>|person-<id>
# Remembered on UserPreference.current_spaces["meals_recipes_from"] (a string; "meals" stays a space id).
FROM_KEY = "meals_recipes_from"


def source_token(source):
    """'all', 'mine', 'library', 'shared', 'space-12', or 'person-34'."""
    return f"{source[0]}-{source[1]}" if isinstance(source, tuple) else source


def parse_source(raw, user):
    """A valid source for this person, or None when `raw` is missing or they may not use it."""
    value = (raw or "").strip()
    if value in ("all", "mine", "library", "shared"):
        return value
    if value.startswith("space-") and value[6:].isdigit():
        space_id = int(value[6:])
        if space_id in space_ids(user):
            return ("space", space_id)
    if value.startswith("person-") and value[7:].isdigit():
        person_id = int(value[7:])
        if sharing.grants_reaching(user, Recipe).filter(created_by_id=person_id).exists():
            return ("person", person_id)
    return None


def remembered_source(user):
    from albright_labs.models import UserPreference
    return (UserPreference.for_user(user).current_spaces or {}).get(FROM_KEY)


def remember_source(user, token):
    from albright_labs.models import UserPreference
    pref, _created = UserPreference.objects.get_or_create(user=user)
    spaces = dict(pref.current_spaces or {})
    if spaces.get(FROM_KEY) == token:
        return
    spaces[FROM_KEY] = token
    pref.current_spaces = spaces
    pref.save(update_fields=["current_spaces", "updated_at"])


def resolve_source(request):
    """The list's source: ?from= when valid, else a leftover ?tab= (library / shared / the space being
    shown), else the one they last picked, else All recipes. A valid ?from= is remembered."""
    user = request.user
    raw = request.GET.get("from")
    if raw is None and request.GET.get("tab") in ("library", "shared", "mine"):
        tab = request.GET["tab"]
        raw = tab if tab in ("library", "shared") else f"space-{request.space.pk}"
    explicit = raw is not None
    source = parse_source(raw, user) if explicit else None
    if source is None:
        return parse_source(remembered_source(user), user) or "all"
    if explicit:
        remember_source(user, source_token(source))
    return source


def source_queryset(user, source):
    """Recipes for this source - only ones this person may see. All is their spaces plus recipes shared
    with them (not the public library). distinct() is the caller's job when they also add annotations."""
    if source == "all":
        return Recipe.objects.filter(Q(space_id__in=space_ids(user)) | Q(pk__in=sharing.shared_ids(user, Recipe)))
    if source == "mine":
        return Recipe.objects.filter(space=circles.personal_space(user))
    if source == "library":
        return library()
    if source == "shared":
        return shared_with(user)
    if source[0] == "space":
        return Recipe.objects.filter(space_id=source[1])
    pks = sharing.grants_reaching(user, Recipe).filter(created_by_id=source[1]).values("object_id")
    return Recipe.objects.filter(pk__in=pks)


def source_label(source, user):
    if source == "all":
        return "All recipes"
    if source == "mine":
        return "Mine"
    if source == "library":
        return "Recipe library"
    if source == "shared":
        return "Shared with me"
    if source[0] == "space":
        for space in circles.visible_spaces(user, APP):
            if space.pk == source[1]:
                return space.name
        return "Mine"
    person = sharing.grants_reaching(user, Recipe).filter(created_by_id=source[1]).select_related("created_by").first()
    who = person.created_by if person else None
    return f"From {(who.first_name or who.get_username()).strip()}" if who else "Shared with me"


def source_choices(user):
    """Dropdown groups: All / Mine / each circle; Shared with me and each person who shared; the library."""
    spaces = [s for s in circles.visible_spaces(user, APP) if s.circle_id]
    first = [{"token": "all", "label": "All recipes"}, {"token": "mine", "label": "Mine"}]
    first += [{"token": f"space-{s.pk}", "label": s.name} for s in spaces]
    groups = [{"label": "", "items": first}]
    people = _sharers(user)
    if people:
        shared = [{"token": "shared", "label": "Shared with me"}]
        shared += [{"token": f"person-{p.pk}", "label": f"From {(p.first_name or p.get_username()).strip()}"}
                   for p in people]
        groups.append({"label": "", "items": shared})
    groups.append({"label": "", "items": [{"token": "library", "label": "Recipe library"}]})
    return groups


def _sharers(user):
    """People who shared a recipe this person can see only because of the share (not a space they see)."""
    pks = list(shared_with(user).values_list("pk", flat=True))
    if not pks:
        return []
    people = {}
    for grant in sharing.grants_reaching(user, Recipe).filter(object_id__in=pks).select_related("created_by"):
        who = grant.created_by
        if who is not None and who.pk != user.pk:
            people.setdefault(who.pk, who)
    return sorted(people.values(), key=lambda u: (u.first_name or u.get_username()).lower())


def source_badge(recipe, user, share=None):
    """Where this card came from when it isn't the person's own personal recipe, or None."""
    mine = space_ids(user)
    if recipe.space_id in mine and recipe.space.user_id == user.pk:
        return None
    if recipe.space_id in mine and recipe.space.circle_id:
        return recipe.space.circle.name
    if share is not None and share.created_by_id:
        return (share.created_by.first_name or share.created_by.get_username()).strip()
    if recipe.visibility == Recipe.LIBRARY:
        return "Recipe library"
    return recipe.space.label if getattr(recipe, "space", None) is not None else None


def readable(user, space=None):
    """Every recipe this person may read: in any space they see, the library, and ones shared with them."""
    return Recipe.objects.filter(Q(space_id__in=space_ids(user)) | Q(visibility=Recipe.LIBRARY)
                                 | Q(pk__in=sharing.shared_ids(user, Recipe)))


def readable_recipe(user, space, pk):
    """(recipe, editable) - editable when it's in a space they see or shared with them to edit; Http404 when they
    can't read it at all."""
    recipe = readable(user).select_related("created_by", "space__circle", "space__user").filter(pk=pk).first()
    if recipe is None:
        raise Http404
    return recipe, recipe.space_id in space_ids(user) or sharing.can_edit(user, recipe)


def own_recipe(space, pk, queryset=None):
    """One of this space's recipes (to change) - anything else is a 404, even one they can read."""
    recipe = (queryset if queryset is not None else Recipe.objects).filter(space=space, pk=pk).first()
    if recipe is None:
        raise Http404
    return recipe


def editable_recipe(user, pk, queryset=None):
    """A recipe this person may change: in a space they see, or shared with them to edit - else 404."""
    recipe = sharing.editable_queryset(user, Recipe, queryset if queryset is not None else Recipe.objects.all()).filter(pk=pk).first()
    if recipe is None:
        raise Http404
    return recipe


COPY_FIELDS = ("title", "description", "ingredients", "steps", "prep_minutes", "cook_minutes", "total_time_minutes",
               "servings", "tags", "standard_tags", "source_url", "source_name", "author", "image_url", "notes")


def save_copy(obj, space, user, fields=COPY_FIELDS):
    """"Save to my recipes": a copy of someone else's recipe in `space` - credited "Saved from <their space>", with the
    original source link - that its new space can edit, rate and plan. The original (and its ratings) stay theirs.
    A public share link saves only what its page shows (share_links.COPY_FIELDS)."""
    copy = Recipe(**{name: getattr(obj, name) for name in fields}, space=space, created_by=user, saved_from=obj,
                  saved_from_name=title(obj.space)[:100], visibility=Recipe.HOUSEHOLD)
    copy.save()
    return copy


def owned_recipe(user, pk, queryset=None):
    """A recipe in a space this person sees - its owners (share it, the library, move it) - else 404."""
    recipe = (queryset if queryset is not None else Recipe.objects).filter(space_id__in=space_ids(user), pk=pk).first()
    if recipe is None:
        raise Http404
    return recipe


# ---------------------------------------------------------------------------
# Ratings - shown only to the people who see the recipe's space
# ---------------------------------------------------------------------------

def stamps(recipes, space, user):
    """{recipe pk: [ratings]} for `user` to see - each recipe's stamps from the people who see ITS space, when the
    user sees that space too. A library or shared recipe from a space they don't see shows only their own stamp."""
    from albright_labs.models import Space
    mine = space_ids(user)
    by_space = {}
    for r in recipes:
        by_space.setdefault(r.space_id, []).append(r.pk)
    out = {}
    for space_id, pks in by_space.items():
        if space_id not in mine:
            ids = {user.pk}
        else:
            here = space if space is not None and space_id == space.pk else \
                Space.objects.select_related("circle", "user").filter(pk=space_id).first()
            ids = member_ids(here) if here else {user.pk}
        for rating in RecipeRating.objects.filter(recipe_id__in=pks, user_id__in=ids).select_related("user").order_by("user_id"):
            out.setdefault(rating.recipe_id, []).append(rating)
    return out


def parse_filters(params, user, space):
    """The rating filters from a query string: {"rated_by": "me" | "anyone" | <member id>, "levels": [1-6],
    "none": bool, "love": bool} - anything else ignored. rated_by is only kept if it's someone who sees the space."""
    ids = member_ids(space)
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


def apply_filters(qs, filters, user, space):
    """Recipes matching the rating filters - judged only by the stamps of the people who see this space.
      rated_by me / a member: their stamp is one of `levels` (or they haven't rated it, with "none");
      rated_by anyone: someone's stamp here is one of `levels` (or nobody here rated it);
      love ("We both love"): at least two people here rated it and every one of them Favorite or Staple."""
    ids = member_ids(space)
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
