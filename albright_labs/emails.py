"""Outgoing email for accounts: invites, password setup and reset links, and
Admin > Integrations' test email. Each kind is a plain-text and an HTML
"Albright Labs" email (templates/emails/<kind>.txt, .html, _subject.txt).

Sending never raises: send() answers (ok, message), so a page can say what
happened. Without Gmail settings in .env (settings.EMAIL_CONFIGURED), nothing
is attempted and the message says what's missing; Admin always shows the link
too, to pass on by hand.

Links always use settings.SITE_URL (the real site), never the request's host.
"""
import logging
import smtplib

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string

log = logging.getLogger(__name__)

NOT_CONFIGURED = ("Email isn't set up: add EMAIL_HOST_USER (the Gmail address) and EMAIL_HOST_PASSWORD "
                  "(a Gmail app password) to .env.")


def site_url(path=""):
    return settings.SITE_URL.rstrip("/") + path


def send(kind, to, context):
    """Send one email of this kind to one address. Returns (ok, message)."""
    if not settings.EMAIL_CONFIGURED:
        return False, NOT_CONFIGURED
    if not to:
        return False, "There's no email address to send to."
    context = {"site_url": site_url(), **context}
    subject = " ".join(render_to_string(f"emails/{kind}_subject.txt", context).split())
    message = EmailMultiAlternatives(subject, render_to_string(f"emails/{kind}.txt", context),
                                     settings.DEFAULT_FROM_EMAIL, [to])
    message.attach_alternative(render_to_string(f"emails/{kind}.html", context), "text/html")
    try:
        message.send()
    except smtplib.SMTPAuthenticationError:
        log.warning("Email %s to %s: Gmail refused the login", kind, to)
        return False, "Gmail refused the login - check EMAIL_HOST_USER and the app password in .env."
    except (smtplib.SMTPException, OSError) as exc:     # OSError: network, DNS, timeout
        log.warning("Email %s to %s failed: %s", kind, to, type(exc).__name__)
        return False, f"The email couldn't be sent ({type(exc).__name__}). Try again, or send the link yourself."
    return True, f"Sent to {to}."


def expires_text(when):
    """ "Friday, November 6 at 3:00 PM" in the site's time zone."""
    from django.utils import timezone
    local = timezone.localtime(when)
    return f"{local:%A, %B} {local.day} at {local:%I:%M %p}".replace(" 0", " ")


def inviter_name(user):
    if user is None:
        return "The Albright Labs admin"
    return user.get_full_name() or user.first_name or user.get_username()
