"""Web Push HTTP endpoints and the service worker / offline page (the rules: albright_labs.push).

  - subscribe / unsubscribe this device (JSON, login required);
  - remove one of my other devices (form or JSON, login required, only my rows);
  - /sw.js at the site root (public, Service-Worker-Allowed: /);
  - /offline/ (public fallback the worker shows when a navigation is offline).
"""
import json

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST
from django.views.decorators.csrf import ensure_csrf_cookie

from . import push
from .models import PushSubscription


def _wants_json(request):
    return (request.headers.get("x-requested-with") == "XMLHttpRequest"
            or "application/json" in request.headers.get("accept", "")
            or request.content_type == "application/json")


def _body(request):
    if request.content_type == "application/json":
        try:
            return json.loads(request.body.decode() or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
    return request.POST


@login_required
@require_POST
def subscribe(request):
    """Save (or refresh) this browser's Push subscription."""
    data = _body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)
    endpoint = (data.get("endpoint") or "").strip()
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    p256dh = (keys.get("p256dh") or data.get("p256dh") or "").strip()
    auth = (keys.get("auth") or data.get("auth") or "").strip()
    if not push.valid_endpoint(endpoint) or not push.valid_key(p256dh) or not push.valid_key(auth):
        return JsonResponse({"ok": False, "error": "Invalid subscription."}, status=400)
    ua = (request.META.get("HTTP_USER_AGENT") or "")[:400]
    now = timezone.now()
    sub, created = PushSubscription.objects.update_or_create(
        endpoint=endpoint,
        defaults={"user": request.user, "p256dh": p256dh, "auth": auth, "user_agent": ua, "last_seen_at": now},
    )
    if created:
        # update_or_create won't set created_at on an existing row; new rows get the default.
        pass
    return JsonResponse({"ok": True, "id": sub.pk, "name": sub.device_name()})


@login_required
@require_POST
def unsubscribe(request):
    """Turn off banners on this browser (delete by endpoint)."""
    data = _body(request)
    if data is None:
        return JsonResponse({"ok": False, "error": "Invalid JSON."}, status=400)
    endpoint = (data.get("endpoint") or "").strip()
    if endpoint:
        PushSubscription.objects.filter(user=request.user, endpoint=endpoint).delete()
    return JsonResponse({"ok": True})


@login_required
@require_POST
def remove(request, pk):
    """Remove one of my devices from the list. 404 if it isn't mine."""
    sub = get_object_or_404(PushSubscription, pk=pk, user=request.user)
    sub.delete()
    if _wants_json(request):
        return JsonResponse({"ok": True})
    messages.success(request, "That device will no longer get banners.")
    return redirect("labs:notification_settings")


# The app shell the worker saves at install (offline viewing - albright_labs.offline): with cache_bust's ?v=, the same
# URLs the pages ask for. The fonts' CSS and files are saved the first time a page loads them.
SHELL = ("style.css", "labs/launcher.css", "labs/launcher.js", "labs/push.js", "labs/offline.js", "labs/offline_view.js",
         "js/app_nav.js", "js/menu.js", "js/md_editor.js", "js/fab_dock.js", "labs/quicksave.js", "labs/notifications.js",
         "labs/share.js", "icons/logo-mark-white.png", "icons/logo-mark-dark.png", "icons/icon-192.png",
         "icons/favicon-32.png")
_version = {"at": 0.0, "value": ""}


def static_hash():
    """A short hash of every static file (name, size, last change) and this worker's template: a deploy that changes
    any of them is a new worker - it replaces the old one and drops its caches. Worked out at most once a minute."""
    import hashlib
    import os
    import time

    from django.contrib.staticfiles import finders
    from django.template.loader import get_template

    now = time.time()
    if _version["value"] and now - _version["at"] < 60:
        return _version["value"]
    digest = hashlib.sha1()
    seen = set()
    for finder in finders.get_finders():
        for path, storage in finder.list([]):
            if path in seen:
                continue
            seen.add(path)
            try:
                stat = os.stat(storage.path(path))
            except (OSError, NotImplementedError):
                continue
            digest.update(f"{path}:{stat.st_size}:{int(stat.st_mtime)};".encode())
    try:
        digest.update(str(int(os.path.getmtime(get_template("albright_labs/sw.js").origin.name))).encode())
    except (OSError, AttributeError):
        pass
    _version.update(at=now, value=digest.hexdigest()[:12])
    return _version["value"]


@require_GET
@never_cache
def service_worker(request):
    """The service worker, scoped to the whole site."""
    from albright_reselling_app.templatetags.cache_bust import static_version
    response = render(request, "albright_labs/sw.js", {"version": static_hash(),
                                                       "shell": [static_version(p) for p in SHELL]},
                      content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/"
    # A short no-cache so a new worker is noticed without waiting a day.
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
@ensure_csrf_cookie
def offline(request):
    """The offline fallback (public; no login). Keeps the site header."""
    return render(request, "albright_labs/offline.html", {"current_app": "labs", "current_app_key": "home",
                                                          "app_label": "Labs", "app_brand": "Albright Labs"})
