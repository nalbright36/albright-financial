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
