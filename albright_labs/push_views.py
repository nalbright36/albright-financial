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


@require_GET
@never_cache
def service_worker(request):
    """The service worker, scoped to the whole site."""
    response = render(request, "albright_labs/sw.js", content_type="application/javascript")
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
