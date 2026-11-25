"""Meals-side metadata and helpers for photos on a recipe.

The file itself is an albright_labs.Attachment (parent: the Recipe). This module stores cover / recipe-card
flags and (in e2) links to an ingredient or a step on RecipeAttachment - never on the global Attachment model.

Permissions follow the recipe: Recipe.can_view / Recipe.can_edit (space members, library readers, share grants).
Public share-link pages never see these helpers - they use share_links.public_recipe, an allow-list that does
not include attachments (the safer option: no anonymous read path).
"""
from django.db import transaction
from django.urls import reverse

from albright_labs import attachments

from .models import Recipe, RecipeAttachment

INGREDIENT_ID_LEN = 16


def _is_recipe_attachment(attachment):
    parent = attachment.parent
    return parent is not None and parent._meta.label == "albright_meals.Recipe"


def ensure_meta(recipe, attachment):
    """The RecipeAttachment row for this file, created if needed. Raises ValueError if it's not on this recipe."""
    if not _is_recipe_attachment(attachment) or attachment.object_id != recipe.pk:
        raise ValueError("That file is not on this recipe.")
    meta, _created = RecipeAttachment.objects.get_or_create(attachment=attachment, defaults={"recipe": recipe})
    if meta.recipe_id != recipe.pk:
        raise ValueError("That file is not on this recipe.")
    return meta


def for_recipe(recipe):
    return RecipeAttachment.objects.filter(recipe=recipe).select_related("attachment").order_by("pk")


def cover_meta(recipe):
    return for_recipe(recipe).filter(is_cover=True).first()


def card_meta(recipe):
    return for_recipe(recipe).filter(is_recipe_card=True).first()


def _describe(attachment):
    return attachments.describe(attachment)


def _cover_row(kind, attachment=None, url="", flagged=False):
    if attachment is not None:
        row = _describe(attachment)
        row.update(kind=kind, attachment=attachment, flagged=flagged)
        return row
    return {"kind": kind, "url": url, "thumb_url": url, "attachment": None, "name": "",
            "id": None, "flagged": flagged, "delete_url": ""}


def cover_for(recipe):
    """{"kind": "attachment"|"image_url"|"none", "url", "thumb_url", "attachment", "name", "delete_url"}."""
    meta = cover_meta(recipe)
    if meta is not None and meta.attachment.is_image:
        return _cover_row("attachment", meta.attachment, flagged=True)
    if recipe.prefer_imported_cover and recipe.image_url:
        return _cover_row("image_url", url=recipe.image_url)
    # No flagged cover: the first uploaded image on the recipe, else the imported image_url.
    first = next((a for a in attachments.for_parent(recipe).order_by("pk") if a.is_image), None)
    if first is not None:
        return _cover_row("attachment", first, flagged=False)
    if recipe.image_url:
        return _cover_row("image_url", url=recipe.image_url)
    return _cover_row("none")


def recipe_card_for(recipe):
    meta = card_meta(recipe)
    if meta is None or not meta.attachment.is_image:
        return None
    row = _describe(meta.attachment)
    row["id"] = meta.attachment_id
    return row


def set_cover(recipe, attachment=None, *, prefer_imported=False):
    """Flag `attachment` as the cover (clearing any other). attachment=None clears the flag so the header
    falls back to the first image, then image_url. prefer_imported=True (Change cover → Imported photo)
    keeps image_url in the header even when uploads exist."""
    with transaction.atomic():
        RecipeAttachment.objects.filter(recipe=recipe, is_cover=True).update(is_cover=False)
        recipe.prefer_imported_cover = bool(prefer_imported and attachment is None)
        recipe.save(update_fields=["prefer_imported_cover"])
        if attachment is not None:
            meta = ensure_meta(recipe, attachment)
            if not attachment.is_image:
                raise ValueError("Only a photo can be the cover.")
            meta.is_cover = True
            meta.save(update_fields=["is_cover"])


def set_recipe_card(recipe, attachment=None):
    """Flag `attachment` as the original recipe card. attachment=None clears it."""
    with transaction.atomic():
        RecipeAttachment.objects.filter(recipe=recipe, is_recipe_card=True).update(is_recipe_card=False)
        if attachment is not None:
            meta = ensure_meta(recipe, attachment)
            if not attachment.is_image:
                raise ValueError("Only a photo can be the recipe card.")
            meta.is_recipe_card = True
            meta.save(update_fields=["is_recipe_card"])


def find_on_recipe(recipe, pk):
    """The Attachment if it belongs to this recipe, else None."""
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return None
    attachment = attachments.for_parent(recipe).filter(pk=pk).first()
    return attachment


# ---------------------------------------------------------------------------
# e2: ingredient / step links
# ---------------------------------------------------------------------------

def ingredient_ids(recipe):
    return {str(row.get("id") or "") for row in (recipe.ingredients or []) if isinstance(row, dict) and row.get("id")}


def _step_entries(recipe):
    """[(index, text)] for real steps, skipping '## ' headings."""
    out = []
    for i, step in enumerate(recipe.steps or []):
        text = str(step)
        if text.startswith("## "):
            continue
        out.append((i, text))
    return out


def cleanup_links(recipe):
    """Drop (or retarget) ingredient/step links that no longer resolve after the recipe was edited."""
    ids = ingredient_ids(recipe)
    steps = dict(_step_entries(recipe))
    text_to_index = {}
    for i, text in steps.items():
        text_to_index.setdefault(text, i)
    for meta in list(for_recipe(recipe)):
        changed = []
        if meta.ingredient_id and meta.ingredient_id not in ids:
            meta.ingredient_id = ""
            changed.append("ingredient_id")
        if meta.step_index is not None:
            current = steps.get(meta.step_index)
            if current == meta.step_text or (meta.step_text and current == meta.step_text):
                pass
            elif meta.step_text and meta.step_text in text_to_index:
                meta.step_index = text_to_index[meta.step_text]
                changed.append("step_index")
            else:
                meta.step_index = None
                meta.step_text = ""
                changed.extend(["step_index", "step_text"])
        if changed:
            meta.save(update_fields=sorted(set(changed)))


def link_to_ingredient(recipe, attachment, ingredient_id):
    ingredient_id = str(ingredient_id or "")
    if ingredient_id not in ingredient_ids(recipe):
        raise ValueError("That ingredient is not on this recipe.")
    with transaction.atomic():
        RecipeAttachment.objects.filter(recipe=recipe, ingredient_id=ingredient_id).update(ingredient_id="")
        meta = ensure_meta(recipe, attachment)
        meta.ingredient_id = ingredient_id
        meta.step_index = None
        meta.step_text = ""
        meta.save(update_fields=["ingredient_id", "step_index", "step_text"])
        return meta


def link_to_step(recipe, attachment, step_index):
    try:
        step_index = int(step_index)
    except (TypeError, ValueError) as exc:
        raise ValueError("That step is not on this recipe.") from exc
    entries = dict(_step_entries(recipe))
    if step_index not in entries:
        raise ValueError("That step is not on this recipe.")
    with transaction.atomic():
        RecipeAttachment.objects.filter(recipe=recipe, step_index=step_index).update(step_index=None, step_text="")
        meta = ensure_meta(recipe, attachment)
        meta.step_index = step_index
        meta.step_text = entries[step_index]
        meta.ingredient_id = ""
        meta.save(update_fields=["ingredient_id", "step_index", "step_text"])
        return meta


def ingredient_photo_map(recipe):
    """ingredient_id -> describe(attachment) for links that still resolve."""
    ids = ingredient_ids(recipe)
    out = {}
    for meta in for_recipe(recipe):
        if meta.ingredient_id and meta.ingredient_id in ids and meta.attachment.is_image:
            out[meta.ingredient_id] = _describe(meta.attachment)
    return out


def step_photo_map(recipe):
    """resolved step_index -> describe(attachment)."""
    cleanup_links(recipe)
    valid = {i for i, _text in _step_entries(recipe)}
    out = {}
    for meta in for_recipe(recipe):
        if meta.step_index is not None and meta.step_index in valid and meta.attachment.is_image:
            out[meta.step_index] = _describe(meta.attachment)
    return out


def decorate_steps(recipe, groups):
    """Copy step_groups and add source_index + photo on each displayed step."""
    photos = step_photo_map(recipe)
    entries = _step_entries(recipe)
    cursor = 0
    out = []
    for group in groups:
        steps = []
        for text in group["steps"]:
            index = entries[cursor][0] if cursor < len(entries) else None
            cursor += 1
            photo = photos.get(index) if index is not None else None
            steps.append({"text": text, "index": index, "photo": photo})
        out.append({**group, "steps": steps})
    return out


def context(user, recipe, editable=None):
    """Template context for the recipe page's photos."""
    can_edit = attachments.can_edit(user, recipe) if editable is None else editable
    att = attachments.section(user, recipe, "meals", editable=can_edit)
    cover = cover_for(recipe)
    card = recipe_card_for(recipe)
    return {
        "recipe_att": att,
        "recipe_cover": cover,
        "recipe_card": card,
        "recipe_photos_editable": can_edit,
        "ingredient_photos": ingredient_photo_map(recipe),
        "step_photos": step_photo_map(recipe),
    }


def connect():
    """Drop stale ingredient/step links after a recipe is saved."""
    from django.db import OperationalError, ProgrammingError
    from django.db.models.signals import post_save

    def _after_save(sender, instance, **kwargs):
        if kwargs.get("raw"):
            return
        try:
            cleanup_links(instance)
        except (ProgrammingError, OperationalError):
            pass  # the RecipeAttachment table does not exist yet (a migration mid-way)

    # weak=False: _after_save is local to connect(); a weak reference would let it be garbage-collected at once.
    post_save.connect(_after_save, sender=Recipe, weak=False, dispatch_uid="meals-recipe-attachment-cleanup")
