""""Request an invite" on the public home page, and Admin > Invite requests.

A visitor leaves an email (and an optional note). One row per email: asking again changes nothing and shows the
same thanks, so the form never tells whether someone already asked. Each new request emails every superuser who
has an email address. Only a salted hash of the visitor's IP is kept.

"Send invite" makes a real Invitation (albright_labs.invites - the same link and email as Admin > Users > Invite
user) with the apps the admin ticks; "Decline" just marks it (nothing is sent).
"""
import hashlib
import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from . import emails, invites
from .models import Invitation, InviteRequest
from .ratelimit import client_ip

log = logging.getLogger(__name__)

# Ticked by default on "Send invite": the household apps the home page shows.
DEFAULT_APPS = ("planner", "meals", "fitness", "notes", "money", "opps")


def ip_hash(request):
    """The visitor's IP, hashed with the site's secret - enough to spot repeats, useless to anyone else."""
    return hashlib.sha256(f"{settings.SECRET_KEY}:invite:{client_ip(request)}".encode()).hexdigest()


def record(request, email, note=""):
    """Save a request (email already validated and lowercased). Returns (row, created). A repeat email is left as
    it was and returns created=False."""
    existing = InviteRequest.objects.filter(email__iexact=email).first()
    if existing:
        return existing, False
    try:
        with transaction.atomic():
            row = InviteRequest.objects.create(email=email, note=note, ip_hash=ip_hash(request))
    except IntegrityError:                     # the same email posted twice at once
        return InviteRequest.objects.get(email__iexact=email), False
    notify_admins(row)
    return row, True


def admin_emails():
    users = get_user_model().objects.filter(is_superuser=True, is_active=True).exclude(email="")
    return sorted({u.email for u in users})


def notify_admins(row):
    """Email each superuser about a new request. Never raises; returns how many emails went."""
    sent = 0
    for to in admin_emails():
        ok, message = emails.send("invite_request", to, {
            "email": row.email, "note": row.note, "requested": emails.expires_text(row.created_at),
            "link": emails.site_url(reverse("labs_admin:invite_requests")), "button": "Review invite requests"})
        if ok:
            sent += 1
        else:
            log.info("Invite request #%s: admin email not sent (%s)", row.pk, message)
    return sent


def send_invite(row, by, apps):
    """Make an Invitation for this request and email it. Returns (invite, raw token, emailed, message)."""
    invite, raw = Invitation.create(email=row.email, apps=apps, created_by=by)
    ok, message = invites.send(invite, raw)
    row.status, row.invitation, row.handled_by, row.handled_at = "invited", invite, by, timezone.now()
    row.save(update_fields=["status", "invitation", "handled_by", "handled_at"])
    return invite, raw, ok, message


def decline(row, by):
    row.status, row.handled_by, row.handled_at = "declined", by, timezone.now()
    row.save(update_fields=["status", "handled_by", "handled_at"])
