"""Invite-only sign-up. A superuser makes an invite (Admin > Users > Invite
user); its link is always shown there to copy (e.g. to text it) and can be
emailed. The link opens the sign-up page (invite_views.signup): the invite's
email, locked, plus a username and password; the account gets exactly the
invite's apps. There's no other way to make an account yourself - without a
usable invite the sign-up page is a 404.
"""
from django.urls import reverse
from django.utils import timezone

from . import emails


def link(raw):
    return emails.site_url(reverse("labs:signup", args=[raw]))


def send(invite, raw):
    """Email the invite. Returns (ok, message); records when it went."""
    ok, message = emails.send("invite", invite.email, {
        "name": invite.name, "inviter": emails.inviter_name(invite.created_by), "email": invite.email,
        "apps": ", ".join(invite.app_labels), "link": link(raw), "button": "Create your account",
        "expires": emails.expires_text(invite.expires_at)})
    if ok:
        invite.last_sent_at = timezone.now()
        invite.save(update_fields=["last_sent_at"])
    return ok, message
