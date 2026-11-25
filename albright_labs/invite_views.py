"""The invite link's sign-up page (/join/<token>/) - see invites.py.

Any token that isn't a pending invite (unknown, used, expired, revoked) is a
plain 404, the same as a page that doesn't exist. Every request counts toward
the per-IP sign-up limit (ratelimit.py), so tokens can't be guessed quickly.
"""
from django.contrib import auth
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache

from albright_control.access import audit

from . import app_access, apps_registry, ratelimit
from .forms import SignupForm
from .models import Invitation


@never_cache
def signup(request, token):
    if ratelimit.blocked(request, "signup"):
        return ratelimit.too_many(request, "signup")
    ratelimit.hit(request, "signup")
    invite = Invitation.find_usable(token)
    if invite is None:
        raise Http404
    if request.user.is_authenticated:
        return render(request, "albright_labs/signup.html", {"invite": invite, "signed_in": True})
    form = SignupForm(request.POST or None, invite=invite)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            # Re-read under a lock: two tabs submitting at once must not both use it.
            invite = Invitation.objects.select_for_update().get(pk=invite.pk)
            if invite.status != "pending":
                raise Http404
            user = get_user_model().objects.create_user(username=form.cleaned_data["username"], email=invite.email,
                                                        password=form.cleaned_data["password1"],
                                                        first_name=invite.name[:150])
            pref = app_access.new_user_preference(user, invite.apps)
            if invite.circle_invite_id:                 # it came with a circle invite: theirs to accept now
                from .circles import attach_signup
                attach_signup(user, invite)
            invite.used_at, invite.used_by = timezone.now(), user
            invite.save(update_fields=["used_at", "used_by"])
        auth.login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        audit(request, "invite_used", f"{user.username} ({invite.email}) signed up with invite #{invite.pk}: "
                                      f"{', '.join(pref.allowed_apps) or 'no apps'}")
        if invite.circle_invite_id:
            return redirect("labs:circles")
        return redirect("labs:root")
    return render(request, "albright_labs/signup.html", {"invite": invite, "form": form})


@never_cache
def share_signup(request, reference):
    """Join from a public recipe share link (albright_meals.share_links): the link counts as an invite from its sharer
    while Admin > Users "Share links count as invites" is on. The same sign-up form (plus first name and email), then
    the account gets Meals (and that setting's apps), a used Invitation from the sharer is recorded, and the recipe
    is saved to their recipes and opened. A bad, expired or turned-off reference is a 404, like a bad invite."""
    from albright_meals import share_links
    from albright_meals.models import RecipeShareSettings

    if ratelimit.blocked(request, "signup"):
        return ratelimit.too_many(request, "signup")
    ratelimit.hit(request, "signup")
    config = RecipeShareSettings.load()
    link = share_links.from_reference(reference) if config.links_are_invites else None
    if link is None:
        raise Http404
    if request.user.is_authenticated:                # already have an account: the page offers "Save to my recipes"
        return redirect("albright_meals:shared_recipe", token=link.token)
    form = SignupForm(request.POST or None, invite=None)
    context = {"form": form, "share": {"title": link.recipe.title, "by": share_links.sharer_name(link),
                                       "back": reverse("albright_meals:shared_recipe", args=[link.token])}}
    if request.method == "POST" and form.is_valid():
        keys = [key for key in config.join_apps if key in apps_registry.grantable_keys()]
        with transaction.atomic():
            user = get_user_model().objects.create_user(username=form.cleaned_data["username"], email=form.email,
                                                        password=form.cleaned_data["password1"],
                                                        first_name=form.first_name[:150])
            pref = app_access.new_user_preference(user, keys)
            invite, _raw = Invitation.create(email=form.email, name=form.first_name, apps=pref.allowed_apps,
                                             created_by=link.created_by, days=1)
            invite.used_at, invite.used_by = timezone.now(), user
            invite.save(update_fields=["used_at", "used_by"])
        auth.login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        audit(request, "invite_used", f"{user.username} ({form.email}) signed up from a recipe share link of "
                                      f"{link.created_by.username} (invite #{invite.pk}): {', '.join(pref.allowed_apps)}")
        copy = share_links.save_copy(link, user)
        if copy is None:
            return redirect("labs:root")
        return redirect("albright_meals:recipe", pk=copy.pk)
    return render(request, "albright_labs/signup.html", context)
