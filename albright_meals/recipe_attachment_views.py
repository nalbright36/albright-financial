"""POST endpoints for a recipe's photos (cover, recipe card, and in e2 a link to a line).

Upload and delete stay on the shared attachment views (parent=meals, id=<recipe pk>). No access is a 404.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect
from django.urls import reverse
from django.views.decorators.http import require_POST

from albright_labs import attachments
from albright_labs.attachment_views import _back, _wants_json

from . import recipe_attachments, scope


def _editable(request, pk):
    recipe = scope.readable(request.user).filter(pk=pk).first()
    if recipe is None or not attachments.can_edit(request.user, recipe):
        raise Http404
    return recipe


def _next(request, recipe):
    if request.POST.get("next"):
        return _back(request)
    return reverse("albright_meals:recipe", args=[recipe.pk])


@login_required
@require_POST
def set_cover(request, pk):
    recipe = _editable(request, pk)
    raw = (request.POST.get("attachment") or "").strip()
    try:
        if raw in ("", "image_url"):
            recipe_attachments.set_cover(recipe, None, prefer_imported=(raw == "image_url" and bool(recipe.image_url)))
            messages.success(request, "Cover cleared." if raw == "" or not recipe.image_url else
                             "Using the imported photo as the cover.")
        else:
            attachment = recipe_attachments.find_on_recipe(recipe, raw)
            if attachment is None:
                raise Http404
            recipe_attachments.set_cover(recipe, attachment)
            messages.success(request, "Cover updated.")
    except ValueError as exc:
        messages.error(request, str(exc))
    if _wants_json(request):
        cover = recipe_attachments.cover_for(recipe)
        return JsonResponse({"ok": True, "cover_id": cover["id"], "cover_url": cover["url"]})
    return redirect(_next(request, recipe))


@login_required
@require_POST
def set_card(request, pk):
    recipe = _editable(request, pk)
    raw = (request.POST.get("attachment") or "").strip()
    try:
        if raw in ("", "clear"):
            recipe_attachments.set_recipe_card(recipe, None)
            messages.success(request, "Recipe card cleared.")
        else:
            attachment = recipe_attachments.find_on_recipe(recipe, raw)
            if attachment is None:
                raise Http404
            recipe_attachments.set_recipe_card(recipe, attachment)
            messages.success(request, "Recipe card updated.")
    except ValueError as exc:
        messages.error(request, str(exc))
    if _wants_json(request):
        card = recipe_attachments.recipe_card_for(recipe)
        return JsonResponse({"ok": True, "card_id": None if card is None else card["id"]})
    return redirect(_next(request, recipe))


@login_required
@require_POST
def link_photo(request, pk):
    """Upload (or pick) a photo and pin it to an ingredient line or a step. Images only."""
    recipe = _editable(request, pk)
    ingredient_id = (request.POST.get("ingredient") or "").strip()
    step_raw = (request.POST.get("step") or "").strip()
    attachment = None
    files = request.FILES.getlist("file")
    if files:
        try:
            attachment = attachments.add(request.user, recipe, files[0])
        except attachments.AttachmentError as exc:
            messages.error(request, str(exc))
            return redirect(_next(request, recipe))
    elif (request.POST.get("attachment") or "").strip():
        attachment = recipe_attachments.find_on_recipe(recipe, request.POST.get("attachment"))
        if attachment is None:
            raise Http404
    if attachment is None:
        messages.error(request, "Choose a photo to attach.")
        return redirect(_next(request, recipe))
    if not attachment.is_image:
        messages.error(request, "Only a photo can be attached to a line or a step.")
        return redirect(_next(request, recipe))
    try:
        if ingredient_id:
            recipe_attachments.link_to_ingredient(recipe, attachment, ingredient_id)
            messages.success(request, "Photo added to that ingredient.")
        elif step_raw != "":
            recipe_attachments.link_to_step(recipe, attachment, step_raw)
            messages.success(request, "Photo added to that step.")
        else:
            messages.error(request, "Pick a line or a step to attach this photo to.")
    except ValueError as exc:
        messages.error(request, str(exc))
    if _wants_json(request):
        return JsonResponse({"ok": True, "id": attachment.pk,
                             "url": attachments.describe(attachment)["url"]})
    return redirect(_next(request, recipe))
