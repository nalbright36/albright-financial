"""Per-request memo for the lookups nearly every view repeats: the user's UserPreference (which apps they can open)
and the spaces they see in an app (circles.visible_spaces). One request used to read them up to ten times - each a
round trip to the database.

Only inside a request (RequestCacheMiddleware sets it up and drops it afterwards): scripts, tests calling services
directly and background jobs always read fresh. Any save or delete of a UserPreference, Circle, CircleMembership or
Space clears it, so a change made during the request is seen by the rest of it.
"""
import contextvars

from django.db.models.signals import post_delete, post_save

_store = contextvars.ContextVar("albright_request_cache", default=None)


def get(key, compute):
    store = _store.get()
    if store is None:
        return compute()
    if key not in store:
        store[key] = compute()
    return store[key]


def clear(*args, **kwargs):
    store = _store.get()
    if store is not None:
        store.clear()


class RequestCacheMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = _store.set({})
        try:
            return self.get_response(request)
        finally:
            _store.reset(token)


def watch(*models):
    """Saving or deleting any of these clears the memo (other apps add theirs: PlannerSettings)."""
    for model in models:
        post_save.connect(clear, sender=model, dispatch_uid=f"request-cache-save-{model._meta.label}")
        post_delete.connect(clear, sender=model, dispatch_uid=f"request-cache-delete-{model._meta.label}")


def connect():
    from .models import Circle, CircleMembership, Space, UserPreference
    watch(UserPreference, Circle, CircleMembership, Space)
