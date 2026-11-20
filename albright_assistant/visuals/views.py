"""Visuals pages and JSON (albright_assistant.urls):

  GET  /assistant/visuals/<id>/data/   the chart, drawn now (render.payload) - the owner, or a saved one shared
  POST /assistant/visuals/<id>/save/   a chat draft -> saved: mode "new" (default) or "replace" (the saved Visual it
                                       came from); at most render.max_saved() saved per person
  GET  /account/visuals/               Account > Visuals: your saved ones, and ones shared with you
  GET  /account/visuals/<id>/          one, big - with Share, Rename and Delete for its owner
  POST /account/visuals/<id>/rename/ and /delete/   owner only
Viewing doesn't need "Can use AI assistant" (a shared Visual reaches people without it); saving from a chat does."""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from albright_labs import sharing

from ..models import Visual
from . import render as vrender

FIELDS = ("title", "summary", "spec", "queries")


def _visible(request, pk):
    visual = Visual.objects.select_related("owner").filter(pk=pk).first()
    if visual is None or not vrender.can_view(request.user, visual):
        raise Http404
    return visual


@never_cache
@require_GET
@login_required
def data(request, pk):
    return JsonResponse(vrender.payload(_visible(request, pk), request.user))


@require_POST
@login_required
def save(request, pk):
    from .. import access
    if not access.allowed(request.user):
        raise Http404
    draft = get_object_or_404(Visual, pk=pk, owner=request.user, saved=False)
    mode = request.POST.get("mode", "new")
    target = vrender.saved_target(draft) if mode == "replace" else None
    replacing = target is not None
    if target is None:
        if Visual.objects.filter(owner=request.user, saved=True).count() >= vrender.max_saved():
            return JsonResponse({"error": f"You can keep {vrender.max_saved()} saved visuals - delete one on "
                                          "Account > Visuals first."}, status=409)
        target = Visual(owner=request.user, saved=True)
    for field in FIELDS:
        setattr(target, field, getattr(draft, field))
    target.version = draft.version
    target.save()
    draft.saved_as = target
    draft.save(update_fields=["saved_as", "updated_at"])
    return JsonResponse({"ok": True, "id": target.pk, "replaced": replacing,
                         "url": vrender.card(target, request.user)["page_url"]})


def _saved_for(user):
    mine = list(Visual.objects.filter(owner=user, saved=True))
    shared = list(sharing.shared_with_queryset(user, Visual, Visual.objects.filter(saved=True)).select_related("owner"))
    return mine, shared


@never_cache
@login_required
def index(request):
    mine, shared = _saved_for(request.user)
    def rows(items):
        return [{**c, "info": json.dumps(c)} for c in (vrender.card(v, request.user) for v in items)]
    return render(request, "albright_assistant/visuals.html", {
        "mine": rows(mine), "shared": rows(shared),
        "limit": vrender.max_saved()})


@never_cache
@login_required
def detail(request, pk):
    visual = _visible(request, pk)
    if not visual.saved:
        raise Http404
    mine = visual.owner_id == request.user.pk
    return render(request, "albright_assistant/visual.html", {
        "visual": visual, "card": vrender.card(visual, request.user),
        "info": json.dumps(vrender.card(visual, request.user)), "mine": mine,
        **({"share": sharing.control(request.user, visual)} if mine else {})})


@require_POST
@login_required
def rename(request, pk):
    visual = get_object_or_404(Visual, pk=pk, owner=request.user, saved=True)
    title = " ".join(request.POST.get("title", "").split())[:120]
    if title:
        visual.title = title
        visual.save(update_fields=["title", "updated_at"])
        messages.success(request, "Renamed.")
    if request.POST.get("next") == "list":
        return redirect("assistant:visuals")
    return redirect("assistant:visual", pk)


@require_POST
@login_required
def delete(request, pk):
    visual = get_object_or_404(Visual, pk=pk, owner=request.user, saved=True)
    title = visual.title
    visual.delete()
    messages.success(request, f"Deleted “{title}”.")
    return redirect("assistant:visuals")
