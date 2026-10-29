"""Password setup and reset links - Django's own password reset flow (signed,
single-use tokens that expire after PASSWORD_RESET_TIMEOUT), with this site's
pages.

  - Admin > Users "Send password setup link" (setup_link / send_setup_link):
    for someone added without a password. Emailed when email is configured
    (settings.EMAIL_CONFIGURED) and they have an address; otherwise the link
    is shown to the superuser to send themselves.
  - "Reset password" on the sign-in page (reset_request): emails a link when
    email is configured, else says to ask the site's owner.
"""
from django.conf import settings
from django.contrib.auth import views as auth_views
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.shortcuts import render
from django.urls import reverse, reverse_lazy
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


def setup_link(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    token = default_token_generator.make_token(user)
    return request.build_absolute_uri(reverse("password_reset_confirm", args=[uid, token]))


def send_setup_link(request, user):
    """Emails the link when possible. Returns (link, emailed: bool)."""
    link = setup_link(request, user)
    if settings.EMAIL_CONFIGURED and user.email:
        days = settings.PASSWORD_RESET_TIMEOUT // 86400
        send_mail("Set your password for Albright Labs",
                  f"Hi {user.first_name or user.get_username()},\n\nAn account was made for you on Albright Labs "
                  f"(username: {user.get_username()}). Choose your password here:\n\n{link}\n\n"
                  f"The link works once and expires in {days} days.\n",
                  None, [user.email])
        return link, True
    return link, False


class ResetRequest(auth_views.PasswordResetView):
    template_name = "registration/reset_request.html"
    email_template_name = "registration/reset_email.txt"
    subject_template_name = "registration/reset_subject.txt"
    success_url = reverse_lazy("password_reset_done")


def reset_request(request):
    if not settings.EMAIL_CONFIGURED:
        return render(request, "registration/reset_request.html", {"email_off": True})
    return ResetRequest.as_view()(request)


class ResetDone(auth_views.PasswordResetDoneView):
    template_name = "registration/reset_sent.html"


class ResetConfirm(auth_views.PasswordResetConfirmView):
    template_name = "registration/reset_confirm.html"
    success_url = reverse_lazy("password_reset_complete")


class ResetComplete(auth_views.PasswordResetCompleteView):
    template_name = "registration/reset_complete.html"
