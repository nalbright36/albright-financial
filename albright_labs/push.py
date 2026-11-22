"""Web Push for site notifications (My account > Notifications, "Notify this device").

A PushSubscription is one browser or phone. After an ActivityEvent is saved, schedule()
sends a banner to everyone who would see that event in the bell, except the actor and
anyone in quiet hours. Missing VAPID keys disable sending (no error).
A merged event (the same person's burst of edits to one thing, activity.record_activity) is ONE banner: it is sent
again to someone only after they've read it (tapped it, or Mark all read) since its last change, or when its last
change fell in their quiet hours (so no banner went then). Nobody gets a banner about a thing they changed themselves
in the last ACTIVE_WINDOW (they're working on it right now). The banner's tag is the event, so a repeat replaces the
old banner instead of stacking, and tapping it marks it read (labs:notification_open). A failed send
never raises to the caller. 404/410 from the push service deletes that subscription.
"""
import json
import logging
import re
from datetime import timedelta
from urllib.parse import urlparse

from django.conf import settings
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from pywebpush import WebPushException, webpush

logger = logging.getLogger(__name__)

# A short timeout so a slow push service cannot stall the request that created the event.
SEND_TIMEOUT = 8
# Someone who changed the same thing this recently is in it right now: no banner about it.
ACTIVE_WINDOW = timedelta(minutes=5)


def vapid_enabled():
    """True when the three VAPID env vars are set (otherwise push is silently off)."""
    return bool(settings.VAPID_PUBLIC_KEY and settings.VAPID_PRIVATE_KEY and settings.VAPID_SUBJECT)


def public_key():
    return (settings.VAPID_PUBLIC_KEY or "").strip()


def subject():
    """VAPID 'sub' claim: a mailto: or https: URL. An email alone gets mailto:."""
    value = (settings.VAPID_SUBJECT or "").strip()
    if value and ":" not in value:
        value = f"mailto:{value}"
    return value


def device_name(user_agent):
    """A short label from a User-Agent, e.g. 'iPhone · Safari'."""
    ua = user_agent or ""
    if "iPhone" in ua:
        device = "iPhone"
    elif "iPad" in ua:
        device = "iPad"
    elif "Android" in ua:
        device = "Android"
    elif "Mac OS" in ua or "Macintosh" in ua:
        device = "Mac"
    elif "Windows" in ua:
        device = "Windows"
    elif "CrOS" in ua:
        device = "Chromebook"
    elif "Linux" in ua:
        device = "Linux"
    else:
        device = "This device"
    # Chrome's UA also contains Safari; check Chrome/Edge/Firefox first.
    if "Edg/" in ua or "EdgiOS/" in ua or "Edge/" in ua:
        browser = "Edge"
    elif "OPR/" in ua or "Opera" in ua:
        browser = "Opera"
    elif "Firefox/" in ua or "FxiOS/" in ua:
        browser = "Firefox"
    elif "CriOS/" in ua or "Chrome/" in ua or "Chromium/" in ua:
        browser = "Chrome"
    elif "Safari/" in ua or "AppleWebKit/" in ua:
        browser = "Safari"
    else:
        browser = ""
    return f"{device} · {browser}" if browser else device


def schedule(event, merged_since=None):
    """Send after the surrounding transaction commits. Never raises. merged_since: see notify_event."""
    if event is None or not getattr(event, "pk", None) or not vapid_enabled():
        return
    pk = event.pk

    def _run():
        try:
            notify_event(pk, merged_since=merged_since)
        except Exception:
            logger.exception("Web push failed for event %s", pk)

    try:
        transaction.on_commit(_run)
    except Exception:
        logger.exception("Web push schedule failed for event %s", pk)


def _busy_with_it(user, event):
    """They changed the same thing themselves in the last ACTIVE_WINDOW (they're in it right now)."""
    from .models import ActivityEvent
    if not event.content_type_id or not event.object_id:
        return False
    return ActivityEvent.objects.filter(actor=user, content_type_id=event.content_type_id, object_id=event.object_id,
                                        updated_at__gte=timezone.now() - ACTIVE_WINDOW).exists()


def _worth_another(user, prefs, event, since):
    """A merged event they were already bannered about: again only if they read it since its last change (`since`),
    or that change fell in their quiet hours (no banner went then)."""
    from . import activity
    from .models import ActivityRead
    if activity.in_quiet_hours(prefs, since):
        return True
    if prefs.read_all_at and prefs.read_all_at >= since:
        return True
    return ActivityRead.objects.filter(user=user, event=event, read_at__gte=since).exists()


def notify_event(event_or_pk, merged_since=None):
    """Send this event to every subscribed person who should be notified. Safe to call directly in tests.
    merged_since: the event grew (record_activity merged into it) - when it last changed before that."""
    from . import activity
    from .models import ActivityEvent, PushSubscription

    if not vapid_enabled():
        return
    if isinstance(event_or_pk, ActivityEvent):
        event = event_or_pk
    else:
        event = ActivityEvent.objects.filter(pk=event_or_pk).select_related("actor", "circle").first()
    if event is None:
        return
    user_ids = list(PushSubscription.objects.exclude(user_id=event.actor_id)
                    .values_list("user_id", flat=True).distinct())
    if not user_ids:
        return
    from django.contrib.auth import get_user_model
    users = list(get_user_model().objects.filter(pk__in=user_ids, is_active=True))
    for user in users:
        prefs = activity.prefs_for(user)
        if activity.in_quiet_hours(prefs):
            continue
        if merged_since is not None and not _worth_another(user, prefs, event, merged_since):
            continue
        if _busy_with_it(user, event):
            continue
        if not activity.notifications(user, prefs).filter(pk=event.pk).exists():
            continue
        body = activity.text(event)
        url = reverse("labs:notification_open", args=[event.pk])
        for sub in PushSubscription.objects.filter(user=user):
            send_to(sub, title="Albright", body=body, url=url, tag=f"event-{event.pk}")


def send_to(sub, *, title, body, url, tag=""):
    """Send one banner. 404/410 drops the subscription. Other errors are logged and ignored.
    tag: a banner with the same tag replaces the last one on the device (sw.js) instead of stacking."""
    if not vapid_enabled():
        return
    payload = json.dumps({"title": title, "body": body, "url": url, **({"tag": tag} if tag else {})})
    try:
        webpush(
            subscription_info={
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            },
            data=payload,
            vapid_private_key=settings.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": subject()},
            timeout=SEND_TIMEOUT,
            ttl=86400,
        )
    except WebPushException as exc:
        status = exc.status_code
        if status in (404, 410):
            type(sub).objects.filter(pk=sub.pk).delete()
            return
        logger.warning("Web push to %s failed: %s", sub.pk, exc)
        return
    except Exception:
        logger.exception("Web push to %s failed", sub.pk)
        return
    type(sub).objects.filter(pk=sub.pk).update(last_seen_at=timezone.now())


def valid_endpoint(value):
    """A push service URL (https, or http on localhost when DEBUG is on, for tests)."""
    if not value or len(value) > 2000:
        return False
    parsed = urlparse(value)
    if parsed.scheme == "https" and parsed.netloc:
        return True
    if settings.DEBUG and parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}:
        return True
    return False


_KEY = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


def valid_key(value):
    return bool(value and _KEY.match(value))
