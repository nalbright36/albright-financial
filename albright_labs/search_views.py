"""The global search ("Option B: Command palette" in docs/design/global_search_options.html - labs/search.js,
labs/search.css). One source: albright_notes.links.find() - the same permission-checked search the note link picker
uses.

  GET  /search/?q=&app=   {"q", "groups": [...], "filters": [{key, label, accent}]} - with no q: also "recent" (the
                          person's last 8 searches) and "suggestions"
  POST /search/recent/    q=... remembers a search (opening a result or "See all"); clear=1 forgets them all"""
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from albright_notes import links

from .models import UserPreference

RECENT_MAX = 8


def _recent(user):
    pref = UserPreference.objects.filter(user=user).first()
    return [q for q in (pref.recent_searches if pref else []) if isinstance(q, str)][:RECENT_MAX]


def _suggestions(user, allowed):
    out = []
    if "planner" in allowed:
        out.append({"label": "Tasks this week", "url": reverse("albright_planner:week"), "icon": "cal"})
    out.append({"label": "Shared with me", "url": reverse("labs:shared_with_me"), "icon": "user"})
    if "meals" in allowed:
        out.append({"label": "Recipes", "url": reverse("albright_meals:recipes"), "icon": "fork"})
    if "notes" in allowed:
        out.append({"label": "Notes", "url": reverse("albright_notes:list"), "icon": "note"})
    return out


@login_required
@never_cache
def search(request):
    user = request.user
    q = " ".join(links.words_of(request.GET.get("q", "")))
    allowed = links._allowed_apps(user)
    filters = [{"key": g.key, "label": g.label, "accent": g.accent} for g in links.groups_for(user, allowed)]
    group = request.GET.get("app", "")
    group = group if any(f["key"] == group for f in filters) else None
    out = {"q": q, "app": group or "", "filters": filters, "groups": links.find(user, q, group) if q else []}
    if q and not out["groups"]:      # "Nothing found" - offer the apps' own searches
        out["elsewhere"] = [{"label": f"Search in {g.label}", "url": g.see_all(q)}
                            for g in links.groups_for(user, allowed) if g.see_all][:3]
    if not q:
        out["recent"] = _recent(user)
        out["suggestions"] = _suggestions(user, allowed)
    return JsonResponse(out)


@login_required
@require_POST
def remember(request):
    pref = UserPreference.objects.get_or_create(user=request.user)[0]
    if request.POST.get("clear") == "1":
        recent = []
    else:
        q = " ".join(links.words_of(request.POST.get("q", "")))
        if len(q) < 2:
            return JsonResponse({"ok": False, "recent": _recent(request.user)}, status=400)
        recent = [q] + [r for r in (pref.recent_searches or []) if isinstance(r, str) and r.lower() != q.lower()]
    pref.recent_searches = recent[:RECENT_MAX]
    pref.save(update_fields=["recent_searches", "updated_at"])
    return JsonResponse({"ok": True, "recent": pref.recent_searches})
