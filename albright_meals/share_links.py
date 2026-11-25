"""Public, read-only recipe share links (RecipeShareLink) - "Copy share link" in a recipe's Share box makes one;
anyone with /meals/r/<token>/ sees the recipe, no sign-in ("Option A" in docs/design/shared_recipe_options.html).

What the page may show is an allow-list built here (public_recipe) - never the Recipe object itself: title,
description, total time, servings (with the scaler), ingredients, steps, nutrition per serving and "Shared by <first
name>". Never ratings, private notes, the source or tags, the space or household, the owner's username or email, or
other recipes.

A link works while it isn't turned off AND its maker can still open Meals and see the recipe's space - someone who
leaves the circle (or loses Meals) stops sharing it.

Join (share link as an invite - RecipeShareSettings): the page's "Join Albright Labs" carries a signed reference to
the link (join_reference) to the sign-up page (albright_labs.invite_views.share_signup), which makes the account,
gives it Meals (and the setting's apps) and saves a copy of the recipe (scope.save_copy) - recorded as a used
Invitation from the sharer. Someone already signed in gets "Save to my recipes" (the same copy)."""
import secrets

from django.core import signing
from django.urls import reverse
from django.utils import timezone

from albright_labs import circles, emails

from . import importer, meal_view, recipe_nutrition, scope
from .models import Recipe, RecipeShareLink, RecipeShareSettings

TOKEN_BYTES = 18                      # secrets.token_urlsafe(18): 24 characters, 144 bits
JOIN_SALT = "albright_meals.share_link.join"
JOIN_MAX_AGE = 14 * 24 * 3600         # a Join reference works for two weeks (the link must still be on)
# What a copy saved from a share link gets: only what the public page showed.
COPY_FIELDS = ("title", "description", "ingredients", "steps", "prep_minutes", "cook_minutes", "total_time_minutes",
               "servings")


def new_token():
    return secrets.token_urlsafe(TOKEN_BYTES)


def active_link(recipe):
    return RecipeShareLink.objects.filter(recipe=recipe, revoked_at__isnull=True).order_by("-created_at").first()


def get_or_create(recipe, user):
    """The recipe's active link, made now (by `user`) if it has none. Returns (link, created)."""
    link = active_link(recipe)
    if link is not None:
        return link, False
    return RecipeShareLink.objects.create(recipe=recipe, created_by=user, token=new_token()), True


def turn_off(recipe):
    """Every active link to this recipe stops working at once."""
    return RecipeShareLink.objects.filter(recipe=recipe, revoked_at__isnull=True).update(revoked_at=timezone.now())


def regenerate(recipe, user):
    """A new link; the old one stops working."""
    turn_off(recipe)
    return RecipeShareLink.objects.create(recipe=recipe, created_by=user, token=new_token())


def url(link):
    return emails.site_url(reverse("albright_meals:shared_recipe", args=[link.token]))


def owner_links(user):
    """The active links to recipes this person's spaces own (Sharing defaults lists them)."""
    return list(RecipeShareLink.objects.filter(revoked_at__isnull=True, recipe__space_id__in=scope.space_ids(user))
                .select_related("recipe").order_by("-created_at"))


def find(token):
    """The active link for this token, or None: unknown, turned off, or its maker can't see the recipe any more."""
    if not token or len(token) > 64:
        return None
    link = RecipeShareLink.objects.filter(token=token, revoked_at__isnull=True) \
        .select_related("recipe__space", "recipe__nutrition", "created_by").first()
    if link is None or not link.created_by.is_active:
        return None
    if not circles.can_see(link.created_by, "meals", link.recipe.space):
        return None
    return link


def count_view(link):
    from django.db.models import F
    RecipeShareLink.objects.filter(pk=link.pk).update(view_count=F("view_count") + 1, last_viewed_at=timezone.now())


def sharer_name(link):
    """Their first name only - never a username or email ("Someone" when they haven't set one)."""
    return (link.created_by.first_name or "").strip().split(" ")[0][:40] or "Someone"


def time_text(minutes):
    if not minutes:
        return ""
    hours, mins = divmod(int(minutes), 60)
    if hours and mins:
        return f"{hours} hr {mins} min"
    return f"{hours} hr" if hours else f"{mins} min"


def _number(quantity):
    try:
        return float(quantity) if quantity is not None else None
    except (TypeError, ValueError):
        return None


def public_recipe(link):
    """Everything the public page shows - and nothing else."""
    recipe = link.recipe
    from .ingredients import group_rows, mode_for
    keep = ("amount", "name", "note", "qty", "qty_min", "unit_one", "unit_many")
    groups = [{"label": g["label"], "category": g["category"], "rows": [{k: row.get(k) for k in keep} for row in g["rows"]]}
              for g in group_rows(meal_view.ingredient_rows(recipe.ingredients), mode_for(None, recipe.ingredients))]
    minutes = recipe.total_time_minutes or ((recipe.prep_minutes or 0) + (recipe.cook_minutes or 0)) or None
    per = recipe_nutrition.per_serving(getattr(recipe, "nutrition", None))
    nutrition = None
    if per and per.get("calories") is not None:
        nutrition = {k: per.get(k) for k in ("calories", "protein", "carbs", "fat", "fiber")}
    return {
        "title": recipe.title,
        "description": recipe.description,
        "time": time_text(minutes),
        "servings": recipe.servings,
        "ingredient_groups": groups,
        "step_groups": importer.step_groups(recipe.steps),
        "nutrition": nutrition,
        "shared_by": sharer_name(link),
    }


def preview_line(public):
    """The link preview's description: "6 hr 10 min · Serves 6 · Shared by Nathan"."""
    return " · ".join(p for p in (public["time"], f"Serves {public['servings']}" if public["servings"] else "",
                                  f"Shared by {public['shared_by']}") if p)


# ---------------------------------------------------------------------------
# Join and save
# ---------------------------------------------------------------------------

def invites_on():
    return RecipeShareSettings.load().links_are_invites


def join_reference(link):
    """A signed reference to the link for the sign-up page (it can't be forged or pointed at another link)."""
    return signing.dumps({"link": link.pk, "t": link.token[:8]}, salt=JOIN_SALT, compress=True)


def from_reference(reference):
    """The still-active link a Join reference points at, or None (tampered, expired, link off)."""
    try:
        data = signing.loads(reference or "", salt=JOIN_SALT, max_age=JOIN_MAX_AGE)
    except signing.BadSignature:
        return None
    link = RecipeShareLink.objects.filter(pk=data.get("link")).select_related("created_by").first()
    if link is None or not link.token.startswith(str(data.get("t", "")) or "\0"):
        return None
    return find(link.token)


def save_copy(link, user):
    """The recipe, saved to this person's current Meals space (scope.save_copy) - credited with the sharer's first
    name, never their space or username. None when they can't open Meals."""
    space = circles.current_space(user, "meals")
    if space is None:
        return None
    existing = Recipe.objects.filter(space=space, saved_from=link.recipe).first()
    if existing is not None:                     # saving twice opens the copy already made
        return existing
    copy = scope.save_copy(link.recipe, space, user, fields=COPY_FIELDS)
    copy.saved_from_name = f"{sharer_name(link)}'s share link"[:100]
    copy.save(update_fields=["saved_from_name"])
    return copy
