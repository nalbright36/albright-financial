"""Per-IP rate limits for the sign-in, sign-up and forgot-password pages, to slow
down password and invite-token guessing. Counts live in the database
(models.RateLimitHit) so every web worker shares them.

  - login: failed sign-ins only
  - signup: every invite page request (a bad token, a form post)
  - reset: forgot-password form posts

Over the limit, the page answers 429 with how long to wait, whatever was
asked - so it never tells whether an account or invite exists.
"""
from datetime import timedelta

from django.shortcuts import render
from django.utils import timezone

from .models import RateLimitHit

# scope: (attempts, window in minutes)
LIMITS = {"login": (10, 15), "signup": (20, 15), "reset": (5, 15)}


def client_ip(request):
    """PythonAnywhere puts the visitor's address in X-Real-IP (REMOTE_ADDR is its own proxy)."""
    ip = (request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR") or "unknown").strip()
    return ip[:45]


def _recent(scope, ip):
    attempts, minutes = LIMITS[scope]
    return RateLimitHit.objects.filter(scope=scope, ip=ip, created_at__gte=timezone.now() - timedelta(minutes=minutes))


def blocked(request, scope):
    """True when this IP has used up its attempts for the scope."""
    return _recent(scope, client_ip(request)).count() >= LIMITS[scope][0]


def hit(request, scope):
    """Count one attempt (and prune day-old rows)."""
    RateLimitHit.objects.filter(created_at__lt=timezone.now() - timedelta(days=1)).delete()
    RateLimitHit.objects.create(scope=scope, ip=client_ip(request))


def clear(request, scope):
    """Forget this IP's attempts (after a successful sign-in)."""
    RateLimitHit.objects.filter(scope=scope, ip=client_ip(request)).delete()


def too_many(request, scope):
    minutes = LIMITS[scope][1]
    response = render(request, "albright_labs/rate_limited.html", {"minutes": minutes}, status=429)
    response["Retry-After"] = str(minutes * 60)
    return response
