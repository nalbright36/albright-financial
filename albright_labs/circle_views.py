"""My account > Circles (albright_labs.circles does every check): my circles and my part in each app, creating one,
invites waiting for me (accept - choosing which apps to join, all off - or decline), and a circle's own page (members,
pending invites, Leave, and the owners' settings). Someone not active in a circle gets a 404 for its page."""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from . import activity_views, apps_registry, circles, invites
from .models import CIRCLE_APPS, Circle, CircleInvite, CircleMembership


def _app_rows(keys):
    return [{"key": k, "label": apps_registry.BY_KEY[k].label, "shares": circles.APP_SHARES[k]} for k in keys
            if k in apps_registry.BY_KEY]


def _initials(user):
    name = (user.get_full_name() or user.get_username()).strip()
    parts = name.split()
    return ((parts[0][:1] + (parts[1][:1] if len(parts) > 1 else "")) or "?").upper()


def _person(user):
    return (user.first_name or user.get_username()).strip()


def _active_circle(user, pk):
    """A circle I'm active in (its page) - anyone else: 404."""
    circle = get_object_or_404(Circle, pk=pk)
    me = CircleMembership.objects.filter(circle=circle, user=user, status=CircleMembership.ACTIVE).first()
    if me is None:
        raise Http404
    return circle, me


@login_required
@never_cache
def circles_page(request):
    user = request.user
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "create":
                circle = circles.create(user, request.POST.get("name", ""), request.POST.getlist("apps"))
                sent = _invite_many(request, circle, request.POST.get("invite", ""))
                messages.success(request, f"Created {circle.name}." + (f" Invited {sent}." if sent else ""))
                return redirect("labs:circle", pk=circle.pk)
            if action == "participate":
                circle = get_object_or_404(Circle, pk=request.POST.get("circle") or 0)
                app, on = request.POST.get("app", ""), request.POST.get("on") == "1"
                circles.set_participation(user, circle, app, on)
                label = apps_registry.BY_KEY[app].label if app in apps_registry.BY_KEY else app
                messages.success(request, f"{label} {'on' if on else 'off'} for {circle.name}.")
        except circles.CircleError as exc:
            messages.error(request, str(exc))
        return redirect(request.POST.get("next") if request.POST.get("next", "").startswith("/home/account/circles") else "labs:circles")
    rows = []
    for circle, me in circles.my_circles(user):
        members = [m.user for m in circles.active_members(circle)]
        rows.append({"circle": circle, "me": me, "members": [(u, _initials(u)) for u in members[:8]], "more": max(0, len(members) - 8),
                     "apps": [{**a, "on": a["key"] in (me.apps or []), "can_open": circles.can_use_app(user, a["key"])}
                              for a in _app_rows(circle.enabled_apps)]})
    return render(request, "albright_labs/circles.html", {
        "rows": rows, "pending": circles.pending_invites(user), "apps": _app_rows([a for a in CIRCLE_APPS if circles.can_use_app(user, a)])})


def _invite_many(request, circle, text):
    """Invites from a box of exact usernames or emails (one per line or comma) -> how many went out."""
    sent = 0
    for who in [w.strip() for w in text.replace(",", "\n").splitlines() if w.strip()][:20]:
        try:
            _ci, _raw, site, site_raw = circles.invite(request.user, circle, who)
        except circles.CircleError as exc:
            messages.error(request, f"{who}: {exc}")
            continue
        sent += 1
        if site is not None:
            ok, note = invites.send(site, site_raw)
            if not ok:
                messages.info(request, f"{who}: the email didn't go ({note}). Their sign-up link: {invites.link(site_raw)}")
    return sent


@login_required
@never_cache
def circle_page(request, pk):
    user = request.user
    circle, me = _active_circle(user, pk)
    owner = me.role == CircleMembership.OWNER
    if request.method == "POST":
        action = request.POST.get("action")
        try:
            if action == "leave":
                circles.leave(user, circle)
                messages.success(request, f"You left {circle.name}. Your own data is untouched.")
                return redirect("labs:circles")
            if not owner:
                raise circles.CircleError("Only the circle's owners can do that.")
            if action == "rename":
                circles.rename(user, circle, request.POST.get("name", ""))
            elif action == "apps":
                circles.set_enabled_apps(user, circle, request.POST.getlist("apps"))
            elif action == "threshold":
                circles.set_money_threshold(user, circle, request.POST.get("amount", ""))
            elif action == "invite":
                sent = _invite_many(request, circle, request.POST.get("who", ""))
                if sent:
                    messages.success(request, f"Invited {sent}.")
                return redirect("labs:circle", pk=circle.pk)
            elif action in ("remove", "make_owner"):
                person = get_object_or_404(CircleMembership, circle=circle, user_id=request.POST.get("user") or 0,
                                           status=CircleMembership.ACTIVE).user
                if action == "remove":
                    circles.remove(user, circle, person)
                else:
                    circles.transfer(user, circle, person, keep_owner=request.POST.get("keep") == "1")
            elif action in ("revoke", "resend"):
                invite = get_object_or_404(CircleInvite, circle=circle, pk=request.POST.get("invite") or 0)
                if action == "revoke":
                    circles.revoke(user, invite)
                else:
                    circles.resend(user, invite)
                    for site in invite.signups.filter(used_at__isnull=True, revoked=False):
                        raw = site.new_token()
                        site.save(update_fields=["token_hash", "expires_at"])
                        invites.send(site, raw)
            elif action == "delete":
                if request.POST.get("confirm_name", "").strip() != circle.name:
                    raise circles.CircleError("Type the circle's name to delete it.")
                name = circle.name
                circles.delete(user, circle)
                messages.success(request, f"Deleted {name}.")
                return redirect("labs:circles")
            messages.success(request, "Saved.")
        except circles.CircleError as exc:
            messages.error(request, str(exc))
        return redirect("labs:circle", pk=circle.pk)
    members = []
    for m in circles.active_members(circle):
        members.append({"m": m, "initials": _initials(m.user), "name": _person(m.user),
                        "apps": [a for a in _app_rows(circle.enabled_apps) if a["key"] in (m.apps or [])]})
    waiting = [i for i in circle.invites.select_related("user") if i.status in ("pending", "expired")] if owner else []
    return render(request, "albright_labs/circle.html", {
        "circle": circle, "me": me, "owner": owner, "members": members, "waiting": waiting,
        "enabled": _app_rows(circle.enabled_apps), "all_apps": _app_rows(CIRCLE_APPS),
        "my_apps": [{**a, "on": a["key"] in (me.apps or [])} for a in _app_rows(circle.enabled_apps)],
        "activity_rows": activity_views.recent_for_circle(user, circle)})


def _invite_owned_by(user, pk):
    """This person's circle invite, whether or not it's still pending (for idempotent Accept / Decline)."""
    return CircleInvite.objects.filter(pk=pk, user=user).select_related("circle", "invited_by").first()


@login_required
@never_cache
def invite_page(request, pk):
    """An invite waiting for me: what the circle shares, which apps to join (all off), Accept or Decline.
    POST is idempotent: already accepted joins the circle page, expired or revoked say so instead of a 404.
    GET of an already-accepted invite goes to the circle (the old email link still works)."""
    user = request.user
    invite = circles.invite_for(user, pk=pk)
    if request.method == "POST":
        found = invite or _invite_owned_by(user, pk)
        if found is None:
            raise Http404
        try:
            if request.POST.get("action") == "decline":
                circles.decline(user, found)
                messages.success(request, f"Declined the invite to {found.circle.name}.")
                return redirect("labs:circles")
            circles.accept(user, found, request.POST.getlist("apps"))
        except circles.CircleError as exc:
            messages.error(request, str(exc))
            return redirect("labs:circles")
        messages.success(request, f"You joined {found.circle.name}.")
        return redirect("labs:circle", pk=found.circle.pk)
    if invite is None:
        found = _invite_owned_by(user, pk)
        if found is None:
            raise Http404
        me = circles.membership(user, found.circle)
        if found.status == "accepted" or (me is not None and me.status == CircleMembership.ACTIVE):
            return redirect("labs:circle", pk=found.circle.pk)
        messages.error(request, "That invite isn't available any more.")
        return redirect("labs:circles")
    return render(request, "albright_labs/circle_invite.html", {
        "invite": invite, "inviter": _person(invite.invited_by) if invite.invited_by else "Someone",
        "apps": [{**a, "can_open": circles.can_use_app(user, a["key"])} for a in _app_rows(invite.circle.enabled_apps)]})


@login_required
def invite_link(request, token):
    """A circle invite's link (from an email): to its accept page - only for the person it was sent to."""
    invite = circles.invite_for(request.user, raw=token)
    if invite is None:
        raise Http404
    return redirect("labs:circle_invite", pk=invite.pk)


def household_redirect(request):
    """The old My account > Household page now lives in Circles."""
    return redirect(reverse("labs:circles"))
