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

from . import activity, apps_registry, connections, sharing, sheet
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
    """Add / change / remove a person or circle share, set Personal vs a circle, or move a
    space-backed item. Anyone who can't see the item gets a 404. Only the owner (or the
    existing mover rule) can change sharing; a circle or space they aren't in is rejected
    and the item is left alone."""
    obj, kind, space_kind = sheet.resolve(label, pk)
    if obj is None or not sheet.can_view(request.user, obj, kind, space_kind):
        raise Http404
    action, result = request.POST.get("action"), {"ok": True}
    try:
        if action == "set_space":
            message, url = sheet.set_space(request.user, obj, request.POST.get("target") or "", space_kind)
            result["message"], result["redirect"] = message, url
        elif action == "set_visibility":
            result["message"] = sharing.set_visibility(request.user, obj, request.POST.get("target") or "")
        elif action == "add":
            if kind is None:
                raise sharing.ShareError("This item is shared by moving it to a circle.")
            target = request.POST.get("target", "person")
            circle_id = target.split(":", 1)[1] if target.startswith("circle:") else None
            user_id = target.split(":", 1)[1] if target.startswith("user:") else None
            who = "" if user_id or circle_id else request.POST.get("who", "")
            grant = sharing.share(request.user, obj, request.POST.get("permission", sharing.VIEW),
                                  circle_id=circle_id, who=who, user_id=user_id)
            result["message"] = sharing.explain(kind, grant)
            activity.shared(grant, request.user)
        elif action == "permission":
            if kind is None:
                raise sharing.ShareError("This item is shared by moving it to a circle.")
            sharing.set_permission(request.user, obj, request.POST.get("grant") or 0, request.POST.get("permission"))
            result["message"] = "Saved."
        elif action == "remove":
            if kind is None:
                raise sharing.ShareError("This item is shared by moving it to a circle.")
            sharing.unshare(request.user, obj, request.POST.get("grant") or 0)
            result["message"] = "Stopped sharing."
        else:
            raise sharing.ShareError("Nothing to do.")
    except sharing.ShareError as exc:
        result = {"ok": False, "error": str(exc)}
    ctx = {"share": sheet.sheet(request.user, obj)}
    fallback = (kind.url(obj) if kind is not None else space_kind.url(obj))
    if _json(request):
        html = render_to_string("albright_labs/_share.html", ctx, request=request)
        return JsonResponse({**result, "html": html}, status=200 if result["ok"] else 400)
    (messages.success if result["ok"] else messages.error)(request, result.get("message") or result.get("error"))
    return redirect(result.get("redirect") or _next(request, fallback))


@login_required
@never_cache
def defaults(request):
    """What new items in each app are shared with - Only me, one of my circles, or specific people (View / Edit)."""
    user = request.user
    from .circles import ALWAYS_ON, can_use_app, find_person
    apps = [a for a in sharing.APPS if a not in ALWAYS_ON and can_use_app(user, a)]     # Visuals have no "new" form
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
        rows.append({"app": app, "label": sharing.app_label(app),
                     "noun": sharing.APPS[app].noun, "default": d, "people": people,
                     "circles": sharing.shareable_circles(user, app), "text": sharing.default_text(user, app)})
    share_links = []
    if can_use_app(user, "meals"):                   # Meals' public recipe links (albright_meals.share_links)
        from albright_meals import share_links as meals_links
        share_links = meals_links.owner_links(user)
    return render(request, "albright_labs/sharing_defaults.html", {"rows": rows, "share_links": share_links})


@login_required
@never_cache
def shared_with_me(request):
    """Everything shared with me across the apps - filter by app and by circle."""
    app = request.GET.get("app") if request.GET.get("app") in sharing.APPS else None
    circle = request.GET.get("circle") if (request.GET.get("circle") or "").isdigit() else None
    rows = sharing.shared_with_me(request.user, app, circle)
    return render(request, "albright_labs/shared_with_me.html", {
        "rows": rows, "app": app, "circle": int(circle) if circle else None,
        "apps": [(a, sharing.app_label(a)) for a in sharing.APPS],
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
    """My account > People: Find people (?q= - connections.find, rate-limited per person), requests for you and sent,
    everyone you're connected with (Remove / Block), and who you've blocked."""
    user = request.user
    q = " ".join(request.GET.get("q", "").split())[:connections.SEARCH_QUERY_MAX]
    found, limited = [], False
    if q:
        limited = _find_limited(request)
        found = [] if limited else connections.find(user, q)
        # Connect / Block take only ids Find people showed this person (no stepping through ids to read names).
        shown = [p["id"] for p in found] + [i for i in request.session.get(SHOWN_KEY, []) if isinstance(i, int)]
        request.session[SHOWN_KEY] = list(dict.fromkeys(shown))[:SHOWN_MAX]
    looks_like_email = "@" in q and bool(connections.EMAIL_SHAPE.match(q))
    return render(request, "albright_labs/people.html", {
        "rows": connections.people_page_rows(user), "q": q, "found": found, "limited": limited,
        "invite_url": _invite_url(user, q) if q and looks_like_email and not found else "",
        "incoming": connections.incoming(user), "outgoing": connections.outgoing(user),
        "blocked": connections.blocked_by_me(user)})


FIND_PER_MINUTE = 30
SHOWN_KEY, SHOWN_MAX = "people_found", 100


def _shown(request, pk):
    """The account behind a Find people result this person was shown (session), still findable - else None."""
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return None
    if pk not in request.session.get(SHOWN_KEY, []):
        return None
    return connections.findable_person(request.user, pk)


def _find_limited(request):
    """Find people: at most FIND_PER_MINUTE searches a minute per person - no fishing through the directory."""
    key = f"people-find:{request.user.pk}"
    n = cache.get(key, 0)
    if n >= FIND_PER_MINUTE:
        return True
    cache.set(key, n + 1, 60)
    return False


def _invite_url(user, email):
    """"Invite them by email" - the existing invite flows: Admin > Users (superusers), else Circles' Invite by email."""
    from urllib.parse import urlencode
    if user.is_superuser:
        return reverse("albright_control:users") + "?" + urlencode({"email": email}) + "#invite-user"
    return reverse("labs:circles") + "?" + urlencode({"invite": email})


def _back(request, anchor=""):
    nxt = request.POST.get("next", "")
    if nxt and url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}):
        return redirect(nxt)
    return redirect(reverse("labs:people") + anchor)


@login_required
@require_POST
def people_connect(request, pk):
    """Connect: a request to someone Find people showed (connections.findable_person - else the same "No one found")."""
    other = _shown(request, pk)
    try:
        if other is None:
            raise connections.ConnectionError("No one found.")
        _row, outcome = connections.send_request(request.user, other)
    except connections.ConnectionError as exc:
        messages.error(request, str(exc))
        return _back(request)
    name = sharing.person_name(other)
    messages.success(request, {"sent": f"Request sent to {name}.", "already_sent": f"Request sent to {name}.",
                               "accepted": f"You're connected with {name}.",
                               "connected": f"You're already connected with {name}."}[outcome])
    return _back(request)


@login_required
@require_POST
def people_request(request, pk, action):
    """Accept / Decline a request for you, or Cancel one you sent."""
    row = connections.request_for(request.user, pk)
    try:
        if row is None or action not in ("accept", "decline", "cancel"):
            raise connections.ConnectionError("That request isn't there any more.")
        other = row.to_user if row.from_user_id == request.user.pk else row.from_user
        getattr(connections, action)(request.user, row)
        messages.success(request, {"accept": f"You're connected with {sharing.person_name(other)}.",
                                   "decline": "Request declined. They aren't told.",
                                   "cancel": "Request cancelled."}[action])
    except connections.ConnectionError as exc:
        messages.error(request, str(exc))
    return _back(request, "#requests")


@login_required
@require_POST
def people_block(request, pk):
    """Block: they can't find or ask you again (and aren't told). Only someone you're connected with, someone who
    asked you, or someone Find people shows."""
    user = request.user
    other = connections.person_if_connected(user, pk) or _shown(request, pk)
    if other is None:
        row = connections.Connection.objects.filter(status=connections.PENDING, to_user=user, from_user_id=pk).first()
        other = row.from_user if row else None
    try:
        connections.block(user, other)
        messages.success(request, f"Blocked {sharing.person_name(other)}. They can't find you or ask to connect.")
    except connections.ConnectionError as exc:
        messages.error(request, str(exc))
    return _back(request)


@login_required
@require_POST
def people_unblock(request, pk):
    other = next((p for p in connections.blocked_by_me(request.user) if str(p.pk) == str(pk)), None)
    if other is not None:
        connections.unblock(request.user, other)
        messages.success(request, f"Unblocked {sharing.person_name(other)}.")
    return _back(request, "#blocked")


@login_required
@require_POST
def privacy(request):
    """My account > Privacy: who can find me (Find people), and whether connection requests are emailed."""
    from .models import UserPreference
    pref = UserPreference.objects.get_or_create(user=request.user)[0]
    choice = request.POST.get("findable")
    if choice in dict(UserPreference.FIND_CHOICES):
        pref.findable = choice
    pref.email_connect_requests = request.POST.get("email_requests") == "1"
    pref.save(update_fields=["findable", "email_connect_requests", "updated_at"])
    messages.success(request, "Privacy saved.")
    return redirect(reverse("labs:account") + "#privacy")


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
