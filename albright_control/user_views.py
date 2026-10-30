"""Admin > Users: which apps each user can open (a checkbox per app from
albright_labs.apps_registry), invites, and a form to add a user with their
apps. Superusers always have every app, so their row is read-only. A user
without access to an app gets a 404 on every page of it
(albright_labs.app_access). New accounts start with no apps.

Invites (albright_labs.invites) are the only way for someone to make their
own account: "Create invite" always shows the link to copy (e.g. to text it),
with "Send by email" beside it. Only the token's hash is stored, so the link
can't be shown again later - "Resend" makes a new one (the old link stops
working) and shows and emails that.

Someone added without a password can't sign in until they follow a password
setup link ("Send password setup link" - albright_labs.password_views):
emailed when this site can send email, and always shown here too."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from albright_labs import app_access, apps_registry, invites, password_views
from albright_labs.forms import CreateUserForm, InviteForm
from albright_labs.models import Invitation, UserPreference

from .access import audit, superuser_required


def _rows():
    users = get_user_model().objects.order_by("username")
    prefs = {p.user_id: p for p in UserPreference.objects.filter(user__in=users)}
    apps = [app for app in apps_registry.APPS if not app.superuser_only]
    rows = []
    for user in users:
        pref = prefs.get(user.pk) or UserPreference(user=user)
        allowed = set(app_access.allowed_keys(user, pref)) if user.is_active else set(pref.allowed_apps or [])
        rows.append({"user": user, "cells": [{"key": app.key, "label": app.label, "on": app.key in allowed}
                                             for app in apps]})
    return rows, apps


def _page(request, status=200, **extra):
    rows, apps = _rows()
    context = {"rows": rows, "apps": apps, "form": CreateUserForm(), "invite_form": InviteForm(),
               "invites": Invitation.objects.select_related("created_by", "used_by")[:50],
               "email_configured": settings.EMAIL_CONFIGURED, **extra}
    return render(request, "albright_control/users.html", context, status=status)


def _show_invite(request, invite, raw, sent=None):
    """The page with this invite's link to copy, and how emailing it went (if it was tried)."""
    return _page(request, new_invite=invite, invite_link=invites.link(raw), invite_token=raw, invite_sent=sent)


def _send_invite(request, invite, raw, action):
    ok, message = invites.send(invite, raw)
    audit(request, action, f"#{invite.pk} {invite.email}: {'emailed' if ok else 'not emailed - ' + message}")
    return ok, message


@superuser_required
def users(request):
    action = request.POST.get("action") if request.method == "POST" else None
    if action == "create":
        form = CreateUserForm(request.POST)
        if not form.is_valid():
            return _page(request, form=form)
        data = form.cleaned_data
        with transaction.atomic():
            # No password: unusable until they set one from a setup link.
            user = get_user_model().objects.create_user(username=data["username"], email=data["email"],
                                                        password=data["password"] or None,
                                                        first_name=data["first_name"])
            pref = app_access.new_user_preference(user, data["apps"])
        audit(request, "user_created", f"{user.username}: {', '.join(pref.allowed_apps) or 'no apps'}"
              + ("" if data["password"] else " (no password yet)"))
        if data["password"]:
            messages.success(request, f"Added {user.username}. Give them their password yourself.")
        else:
            messages.success(request, f'Added {user.username}. Use "Send password setup link" so they can '
                                      "choose a password.")
        return redirect("albright_control:users")
    if action == "setup_link":
        user = get_object_or_404(get_user_model(), pk=request.POST.get("user_id"), is_active=True)
        link, emailed, message = password_views.send_setup_link(request, user)
        audit(request, "password_setup_link", f"{user.username}: {'emailed' if emailed else 'not emailed - ' + message}")
        return _page(request, setup_link=link, setup_user=user, setup_emailed=emailed, setup_message=message,
                     setup_days=settings.PASSWORD_RESET_TIMEOUT // 86400)
    if action == "access":
        user = get_object_or_404(get_user_model(), pk=request.POST.get("user_id"))
        if user.is_superuser:
            messages.error(request, "Superusers always have every app.")
            return redirect("albright_control:users")
        pref = UserPreference.objects.filter(user=user).first() or UserPreference(user=user)
        before = list(pref.allowed_apps or [])
        app_access.grant(pref, request.POST.getlist("apps"))
        pref.save()
        audit(request, "user_access_changed",
              f"{user.username}: {', '.join(before) or 'none'} -> {', '.join(pref.allowed_apps) or 'none'}")
        messages.success(request, f"Saved {user.username}'s access.")
        return redirect("albright_control:users")
    if action == "invite":
        invite_form = InviteForm(request.POST)
        if not invite_form.is_valid():
            return _page(request, invite_form=invite_form, invite_open=True, status=400)
        data = invite_form.cleaned_data
        keys = [key for key in apps_registry.grantable_keys() if key in set(data["apps"])]
        invite, raw = Invitation.create(email=data["email"], name=data["name"], apps=keys, created_by=request.user,
                                        days=data["days"])
        audit(request, "invite_created", f"#{invite.pk} {invite.email}: {', '.join(keys) or 'no apps'}, "
                                         f"expires in {data['days']} days")
        return _show_invite(request, invite, raw)
    if action == "invite_email":
        # "Send by email" beside a just-made link: the raw token comes back from that panel
        # (it's never stored) and must match the invite.
        invite = get_object_or_404(Invitation, pk=request.POST.get("invite_id"))
        raw = request.POST.get("token", "")
        if not invite.matches(raw) or invite.status != "pending":
            messages.error(request, "That invite link isn't current any more - use Resend for a new one.")
            return redirect("albright_control:users")
        sent = _send_invite(request, invite, raw, "invite_sent")
        return _show_invite(request, invite, raw, sent)
    if action == "invite_resend":
        invite = get_object_or_404(Invitation, pk=request.POST.get("invite_id"))
        if invite.status not in ("pending", "expired"):
            messages.error(request, f"That invite is {invite.status}, so it can't be resent.")
            return redirect("albright_control:users")
        raw = invite.new_token()
        invite.save(update_fields=["token_hash", "expires_at"])
        sent = _send_invite(request, invite, raw, "invite_resent")
        return _show_invite(request, invite, raw, sent)
    if action == "invite_revoke":
        invite = get_object_or_404(Invitation, pk=request.POST.get("invite_id"))
        if invite.status == "pending" or invite.status == "expired":
            invite.revoked = True
            invite.save(update_fields=["revoked"])
            audit(request, "invite_revoked", f"#{invite.pk} {invite.email}")
            messages.success(request, f"Revoked the invite for {invite.email}. Its link no longer works.")
        else:
            messages.error(request, f"That invite is already {invite.status}.")
        return redirect("albright_control:users")
    return _page(request)
