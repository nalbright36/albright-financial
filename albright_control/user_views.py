"""Admin > Users: which apps each user can open (a checkbox per app from
albright_labs.apps_registry), and a form to add a user with their apps.
Superusers always have every app, so their row is read-only. A user without
access to an app gets a 404 on every page of it (albright_labs.app_access).

Someone added without a password can't sign in until they follow a password
setup link ("Send password setup link" - albright_labs.password_views):
emailed when this site can send email, otherwise shown here to pass on.
Someone given exactly one app lands on it after signing in."""
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render

from albright_labs import app_access, apps_registry, password_views
from albright_labs.forms import CreateUserForm
from albright_labs.models import UserPreference

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


@superuser_required
def users(request):
    form = CreateUserForm()
    if request.method == "POST" and request.POST.get("action") == "create":
        form = CreateUserForm(request.POST)
        if form.is_valid():
            data = form.cleaned_data
            with transaction.atomic():
                # No password: unusable until they set one from a setup link.
                user = get_user_model().objects.create_user(username=data["username"], email=data["email"],
                                                            password=data["password"] or None,
                                                            first_name=data["first_name"])
                pref = UserPreference(user=user)
                pref.allowed_apps = []
                app_access.grant(pref, data["apps"])
                only = apps_registry.BY_KEY[pref.allowed_apps[0]] if len(pref.allowed_apps) == 1 else None
                if only and only.pages:
                    # One app (e.g. Meals only): land on its first page, not Home.
                    pref.landing, pref.landing_page = "page", only.pages[0][0]
                pref.save()
            audit(request, "user_created", f"{user.username}: {', '.join(pref.allowed_apps) or 'no apps'}"
                  + ("" if data["password"] else " (no password yet)"))
            if data["password"]:
                messages.success(request, f"Added {user.username}. Give them their password yourself.")
            else:
                messages.success(request, f'Added {user.username}. Use "Send password setup link" so they can '
                                          "choose a password.")
            return redirect("albright_control:users")
    elif request.method == "POST" and request.POST.get("action") == "setup_link":
        user = get_object_or_404(get_user_model(), pk=request.POST.get("user_id"), is_active=True)
        link, emailed = password_views.send_setup_link(request, user)
        audit(request, "password_setup_link", f"{user.username}: {'emailed' if emailed else 'shown to send by hand'}")
        if emailed:
            messages.success(request, f"Emailed a password setup link to {user.username}.")
            return redirect("albright_control:users")
        rows, apps = _rows()
        return render(request, "albright_control/users.html", {
            "rows": rows, "apps": apps, "form": form, "setup_link": link, "setup_user": user,
            "setup_days": settings.PASSWORD_RESET_TIMEOUT // 86400,
            "email_reason": "it has no email address" if settings.EMAIL_CONFIGURED else "this site can't send email"})
    elif request.method == "POST" and request.POST.get("action") == "access":
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
    rows, apps = _rows()
    return render(request, "albright_control/users.html", {"rows": rows, "apps": apps, "form": form})
