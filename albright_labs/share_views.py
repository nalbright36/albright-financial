"""Sharing pages and the Share control's endpoint (albright_labs.sharing does every check):
  - share: add / change / remove a share on one thing (owner only; anyone who can't see it gets a 404);
  - My account > People: connected people (circles, past shares) and Remove (albright_labs.connections);
  - people_search: type-ahead over the connected set and your shareable circles only;
  - My account > Circles > Sharing defaults: what new items in each app are shared with;
  - My account > Circles > Shared with me: everything others shared with me, by app and circle."""
from django.apps import apps as django_apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST

from . import activity, apps_registry, connections, sharing
from .models import SharingDefault
from .ratelimit import client_ip

SEARCH_PER_MINUTE = 60


def _json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _next(request, fallback):
    target = request.POST.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
        return target
    return fallback


@login_required
@require_POST
def share(request, label, pk):
    kind = sharing.KINDS.get(label)
    if kind is None:
        raise Http404
    obj = kind.model.objects.filter(pk=pk).first()
    if obj is None or not sharing.can_view(request.user, obj):
        raise Http404
    action, result = request.POST.get("action"), {"ok": True}
    try:
        if action == "add":
            target = request.POST.get("target", "person")
            circle_id = target.split(":", 1)[1] if target.startswith("circle:") else None
            user_id = target.split(":", 1)[1] if target.startswith("user:") else None
            who = "" if user_id or circle_id else request.POST.get("who", "")
            grant = sharing.share(request.user, obj, request.POST.get("permission", sharing.VIEW),
                                  circle_id=circle_id, who=who, user_id=user_id)
            result["message"] = sharing.explain(kind, grant)
            activity.shared(grant, request.user)
        elif action == "permission":
            sharing.set_permission(request.user, obj, request.POST.get("grant") or 0, request.POST.get("permission"))
            result["message"] = "Saved."
        elif action == "remove":
            sharing.unshare(request.user, obj, request.POST.get("grant") or 0)
            result["message"] = "Stopped sharing."
        else:
            raise sharing.ShareError("Nothing to do.")
    except sharing.ShareError as exc:
        result = {"ok": False, "error": str(exc)}
    if _json(request):
        html = render_to_string("albright_labs/_share.html", {"share": sharing.control(request.user, obj)}, request=request)
        return JsonResponse({**result, "html": html}, status=200 if result["ok"] else 400)
    (messages.success if result["ok"] else messages.error)(request, result.get("message") or result.get("error"))
    return redirect(_next(request, kind.url(obj)))


@login_required
@never_cache
def defaults(request):
    """What new items in each app are shared with - Only me, one of my circles, or specific people (View / Edit)."""
    user = request.user
    from .circles import can_use_app, find_person
    apps = [a for a in sharing.APPS if can_use_app(user, a)]
    if request.method == "POST":
        app = request.POST.get("app")
        if app in apps:
            mode, circle_id, people = request.POST.get("mode", SharingDefault.ONLY_ME), None, {}
            perm = request.POST.get("permission", sharing.VIEW)
            if mode.startswith("circle:"):
                mode, circle_id = SharingDefault.CIRCLE, mode.split(":", 1)[1]
            elif mode == SharingDefault.PEOPLE:
                existing = sharing.default_for(user, app)
                people = dict(existing.people) if existing and existing.mode == SharingDefault.PEOPLE else {}
                for key in request.POST.getlist("drop"):
                    people.pop(key, None)
                who = request.POST.get("who", "").strip()
                if who:
                    person = find_person(who)
                    if person is None or person.pk == user.pk or not can_use_app(person, app):
                        messages.error(request, "No one who can open that app matches that exactly.")
                        return redirect("labs:sharing_defaults")
                    people[str(person.pk)] = perm
            try:
                sharing.save_default(user, app, mode, circle_id, perm, people)
                messages.success(request, f"New {apps_registry.BY_KEY[app].label} items: {sharing.default_text(user, app)}.")
            except sharing.ShareError as exc:
                messages.error(request, str(exc))
        return redirect(reverse("labs:sharing_defaults") + f"#default-{app}")
    rows = []
    User = django_apps.get_model("auth", "User")
    for app in apps:
        d = sharing.default_for(user, app)
        people = []
        if d and d.mode == SharingDefault.PEOPLE:
            by_id = User.objects.in_bulk([int(k) for k in d.people if str(k).isdigit()])
            people = [{"key": k, "name": sharing.person_name(by_id[int(k)]), "permission": v}
                      for k, v in d.people.items() if str(k).isdigit() and int(k) in by_id]
        rows.append({"app": app, "label": "Recipes" if app == "meals" else apps_registry.BY_KEY[app].label,
                     "noun": sharing.APPS[app].noun, "default": d, "people": people,
                     "circles": sharing.shareable_circles(user, app), "text": sharing.default_text(user, app)})
    return render(request, "albright_labs/sharing_defaults.html", {"rows": rows})


@login_required
@never_cache
def shared_with_me(request):
    """Everything shared with me across the apps - filter by app and by circle."""
    app = request.GET.get("app") if request.GET.get("app") in sharing.APPS else None
    circle = request.GET.get("circle") if (request.GET.get("circle") or "").isdigit() else None
    rows = sharing.shared_with_me(request.user, app, circle)
    return render(request, "albright_labs/shared_with_me.html", {
        "rows": rows, "app": app, "circle": int(circle) if circle else None,
        "apps": [(a, "Recipes" if a == "meals" else apps_registry.BY_KEY[a].label) for a in sharing.APPS],
        "circles": sharing.circles_for_filter(request.user)})


# ---------------------------------------------------------------------------
# People (My account > People) and the share picker's search
# ---------------------------------------------------------------------------

def _search_limited(request):
    """True when this user+IP has used up SEARCH_PER_MINUTE lookups in the last minute."""
    key = f"people-search:{request.user.pk}:{client_ip(request)}"
    n = cache.get(key, 0)
    if n >= SEARCH_PER_MINUTE:
        return True
    cache.set(key, n + 1, 60)
    return False


@login_required
@never_cache
def people_page(request):
    """My account > People: everyone you're connected with, and Remove for anyone who isn't also a circle-mate."""
    return render(request, "albright_labs/people.html", {"rows": connections.people_page_rows(request.user)})


@login_required
@require_POST
def people_remove(request, pk):
    """Remove a connection: hide person shares both ways (restored if you reconnect)."""
    other = connections.person_if_connected(request.user, pk)
    try:
        # person_if_connected returns None for a stranger - remove() gives the same vague error
        connections.remove(request.user, other)
        messages.success(request, f"{sharing.person_name(other)} was removed. Your shares with each other are "
                                  "hidden until you reconnect.")
    except connections.ConnectionError as exc:
        messages.error(request, str(exc))
    return redirect("labs:people")


@login_required
@require_GET
@never_cache
def people_search(request):
    """JSON for the Share type-ahead: only this user's connected people and shareable circles. Never all users."""
    if _search_limited(request):
        response = JsonResponse({"circles": [], "people": [], "error": "Too many searches - try again in a minute."},
                                status=429)
        response["Retry-After"] = "60"
        return response
    app = request.GET.get("app") if request.GET.get("app") in sharing.APPS else next(iter(sharing.APPS), "planner")
    obj = None
    label, pk = request.GET.get("label") or "", request.GET.get("pk") or ""
    if label in sharing.KINDS and str(pk).isdigit():
        kind = sharing.KINDS[label]
        obj = kind.model.objects.filter(pk=int(pk)).first()
        if obj is not None and not sharing.is_owner(request.user, obj):
            obj = None                                   # already-shared flags only for the owner
    data = connections.search(request.user, request.GET.get("q", ""), app, obj=obj)
    return JsonResponse(data)
