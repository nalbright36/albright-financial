"""Superuser-only access for the Admin area: anyone else - anonymous,
logged in but not a superuser - gets a plain 404, as if it didn't exist."""
import functools

from django.http import Http404

from .models import AuditEntry


def superuser_required(view):
    @functools.wraps(view)
    def wrapper(request, *args, **kwargs):
        user = getattr(request, "user", None)
        if not (user and user.is_authenticated and user.is_active and user.is_superuser):
            raise Http404
        return view(request, *args, **kwargs)
    return wrapper


def audit(request, action, details=""):
    AuditEntry.objects.create(user=request.user if request.user.is_authenticated else None, action=action,
                              details=str(details)[:500])
