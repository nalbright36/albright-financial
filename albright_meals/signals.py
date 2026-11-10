"""Meals activity (albright_labs.activity) - only in a circle's space, never a Personal one:
  - a recipe added to (or moved into) a circle's kitchen - seen by whoever can view the recipe;
  - the meal plan: a meal planned, moved or removed;
  - the grocery list: lines added, checked off or cleared (repeats by one person merge: "Kate added 5 items");
  - ratings added or changed - seen only within the recipe's space, like the ratings themselves.
Who did it: the signed-in person or the Bot API's token (activity.acting), else the row's own author.
Connected in AlbrightMealsConfig.ready()."""
from django.db.models.signals import post_delete, post_save, pre_save
from django.urls import reverse

from .models import SLOTS, GroceryItem, MealPlanEntry, Recipe, RecipeRating

SLOT_NAMES = dict(SLOTS)


def _circle(space_id):
    from albright_labs.models import Space
    space = Space.objects.select_related("circle").filter(pk=space_id).first() if space_id else None
    return space.circle if space is not None and space.circle_id else None


def _record(space_id, verb, summary, actor=None, group="", plural="", url="", count=1):
    circle = _circle(space_id)
    if circle is None:
        return None
    from albright_labs import activity
    return activity.record_activity("meals", verb, summary, circle=circle, actor=actor, group=group, plural=plural,
                                    url=url, count=count)


def _before(sender, instance, **kwargs):
    """Remember what a row was, so post_save can tell what changed."""
    if instance.pk and not kwargs.get("raw"):
        instance._activity_before = sender.objects.filter(pk=instance.pk).values().first()


def _who(fallback):
    """The acting person when there is one (and the Bot API's "via"), else the row's own author."""
    from albright_labs import activity
    return None if activity.current()[0] is not None else fallback


def _q(text):
    from albright_labs.activity import quote
    return quote(text)


def recipe_saved(sender, instance, created=False, raw=False, **kwargs):
    if raw:
        return
    before = getattr(instance, "_activity_before", None)
    if not created and (before is None or before["space_id"] == instance.space_id):
        return
    circle = _circle(instance.space_id)
    if circle is None:
        return
    from albright_labs import activity
    verb = "created" if created else "moved"
    activity.record_activity("meals", verb, f"added the recipe {_q(instance.title)} to the {circle.name} kitchen",
                             obj=instance, circle=circle, actor=_who(instance.created_by), group="recipe-added", plural=f"added {{n}} recipes to the {circle.name} kitchen")


def grocery_saved(sender, instance, created=False, raw=False, **kwargs):
    if raw:
        return
    url = reverse("albright_meals:groceries") + f"?space={instance.space_id}"
    if created:
        _record(instance.space_id, "added", f"added {_q(instance.name)} to the grocery list", actor=_who(instance.added_by),
                group="grocery-added", plural="added {n} items to the grocery list", url=url)
        return
    before = getattr(instance, "_activity_before", None) or {}
    if instance.checked and not before.get("checked"):
        _record(instance.space_id, "checked", f"checked off {_q(instance.name)} on the grocery list",
                actor=_who(instance.checked_by), group="grocery-checked", plural="checked off {n} grocery items", url=url)
    elif instance.archived_at and not before.get("archived_at"):
        _record(instance.space_id, "cleared", f"cleared {_q(instance.name)} from the grocery list",
                group="grocery-cleared", plural="cleared {n} items from the grocery list", url=url)


def groceries_cleared(space_id, count, actor=None):
    """"Clear checked items" archives lines in one query (no signals): one event for the lot."""
    if count:
        _record(space_id, "cleared", f"cleared {count} checked item{'s' if count != 1 else ''} from the grocery list",
                actor=actor, url=reverse("albright_meals:groceries") + f"?space={space_id}")


def _meal(entry):
    return f"{_q(entry.display_title)} for {SLOT_NAMES.get(entry.slot, entry.slot).lower()} on {entry.date:%a %b} {entry.date.day}"


def plan_saved(sender, instance, created=False, raw=False, **kwargs):
    if raw:
        return
    url = reverse("albright_meals:week") + f"?space={instance.space_id}&start={instance.date.isoformat()}"
    before = getattr(instance, "_activity_before", None) or {}
    if created:
        summary = f"planned {_meal(instance)}"
    elif (before.get("date"), before.get("slot")) != (instance.date, instance.slot):
        summary = f"moved {_meal(instance)}"
    elif (before.get("recipe_id"), before.get("title")) != (instance.recipe_id, instance.title):
        summary = f"changed the plan: {_meal(instance)}"
    else:
        return                                   # notes or cooked / skipped - not worth telling anyone
    _record(instance.space_id, "planned", summary, actor=_who(instance.created_by), group="plan", plural="changed {n} meals in the plan", url=url)


def plan_deleted(sender, instance, **kwargs):
    _record(instance.space_id, "unplanned", f"removed {_meal(instance)} from the plan", group="plan",
            plural="changed {n} meals in the plan", url=reverse("albright_meals:week") + f"?space={instance.space_id}")


def rating_saved(sender, instance, created=False, raw=False, **kwargs):
    if raw:
        return
    before = getattr(instance, "_activity_before", None) or {}
    if not created and before.get("level") == instance.level:
        return
    recipe = instance.recipe
    label = dict(RecipeRating.LEVELS).get(instance.level, "")
    verb = "rated" if created else "re-rated"
    # Seen only by the recipe's space (a circle's), like the ratings themselves - never people it's shared with.
    _record(recipe.space_id, "rated", f"{verb} {_q(recipe.title)} “{label}”", actor=instance.user,
            url=reverse("albright_meals:recipe", args=[recipe.pk]))


def connect():
    for model in (Recipe, GroceryItem, MealPlanEntry, RecipeRating):
        pre_save.connect(_before, sender=model, dispatch_uid=f"meals-activity-before-{model.__name__}")
    post_save.connect(recipe_saved, sender=Recipe, dispatch_uid="meals-activity-recipe")
    post_save.connect(grocery_saved, sender=GroceryItem, dispatch_uid="meals-activity-grocery")
    post_save.connect(plan_saved, sender=MealPlanEntry, dispatch_uid="meals-activity-plan")
    post_delete.connect(plan_deleted, sender=MealPlanEntry, dispatch_uid="meals-activity-plan-deleted")
    post_save.connect(rating_saved, sender=RecipeRating, dispatch_uid="meals-activity-rating")
