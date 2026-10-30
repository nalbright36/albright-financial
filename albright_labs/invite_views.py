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
from django.utils import timezone
from django.views.decorators.cache import never_cache

from albright_control.access import audit

from . import app_access, ratelimit
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
            invite.used_at, invite.used_by = timezone.now(), user
            invite.save(update_fields=["used_at", "used_by"])
        auth.login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        audit(request, "invite_used", f"{user.username} ({invite.email}) signed up with invite #{invite.pk}: "
                                      f"{', '.join(pref.allowed_apps) or 'no apps'}")
        return redirect("labs:root")
    return render(request, "albright_labs/signup.html", {"invite": invite, "form": form})
