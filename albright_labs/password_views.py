"""Password setup and reset links - Django's own password reset flow (signed,
single-use tokens that expire after PASSWORD_RESET_TIMEOUT), with this site's
pages and emails (albright_labs.emails). Links always use settings.SITE_URL.

  - Admin > Users "Send password setup link" (setup_link / send_setup_link):
    for someone added without a password, or who's lost theirs. Emailed when
    email is configured and they have an address; the link is always shown
    to the superuser too, to pass on by hand.
  - "Forgot password?" on the sign-in page (reset_request): rate limited per
    IP, and the answer is the same whether or not the address has an account.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.forms import PasswordResetForm
from django.contrib.auth.tokens import default_token_generator
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from albright_control.models import AuditEntry

from . import emails, ratelimit

log = logging.getLogger(__name__)


def setup_link(user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return emails.site_url(reverse("password_reset_confirm", args=[uid, token]))


def _link_context(user, link, button):
    return {"name": user.first_name or user.get_username(), "username": user.get_username(), "link": link,
            "button": button,
            "expires": emails.expires_text(timezone.now() + timedelta(seconds=settings.PASSWORD_RESET_TIMEOUT))}


def send_setup_link(request, user):
    """Emails the link when possible. Returns (link, emailed: bool, message)."""
    link = setup_link(user)
    if not user.email:
        return link, False, "They have no email address."
    ok, message = emails.send("password_setup", user.email,
                              {**_link_context(user, link, "Choose your password"),
                               "inviter": emails.inviter_name(request.user)})
    return link, ok, message


class ResetForm(PasswordResetForm):
    """Django's reset form (it finds active accounts with a usable password for the address,
    and does nothing - silently - for any other address), sending through emails.send."""

    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email,
                  html_email_template_name=None):
        user = context["user"]
        link = emails.site_url(reverse("password_reset_confirm", args=[context["uid"], context["token"]]))
        ok, message = emails.send("password_reset", to_email, _link_context(user, link, "Choose a new password"))
        AuditEntry.objects.create(user=user, action="password_reset_sent",
                                  details=f"{user.get_username()}: {'emailed' if ok else 'failed - ' + message}"[:500])


class ResetRequest(auth_views.PasswordResetView):
    template_name = "registration/reset_request.html"
    form_class = ResetForm
    success_url = reverse_lazy("password_reset_done")


def reset_request(request):
    if not settings.EMAIL_CONFIGURED:
        return render(request, "registration/reset_request.html", {"email_off": True})
    if request.method == "POST":
        if ratelimit.blocked(request, "reset"):
            return ratelimit.too_many(request, "reset")
        ratelimit.hit(request, "reset")
    return ResetRequest.as_view()(request)


class ResetDone(auth_views.PasswordResetDoneView):
    template_name = "registration/reset_sent.html"


class ResetConfirm(auth_views.PasswordResetConfirmView):
    template_name = "registration/reset_confirm.html"
    success_url = reverse_lazy("password_reset_complete")


class ResetComplete(auth_views.PasswordResetCompleteView):
    template_name = "registration/reset_complete.html"
