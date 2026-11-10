"""Circle fitness challenge pages (/fitness/challenges/). Opening Fitness is required first
(app_access 404s everyone else). A challenge itself is only for active members of its circle
who opted into Fitness there - anyone else gets a 404, including other Fitness users.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import challenges as ch
from .challenges import ACTIVITIES, Challenge, ChallengeEntry, ChallengeError


def _challenge(request, pk):
    challenge = get_object_or_404(Challenge.objects.select_related("circle", "created_by"), pk=pk)
    if not ch.can_participate(request.user, challenge.circle):
        raise Http404
    return challenge


def _entry(request, pk):
    entry = get_object_or_404(ChallengeEntry.objects.select_related("challenge__circle", "user"), pk=pk)
    if not ch.can_participate(request.user, entry.challenge.circle):
        raise Http404
    return entry


def _board(challenge, user=None):
    rows = ch.leaderboard(challenge)
    if user is not None:
        for row in rows:
            row["me"] = row["user"].pk == user.pk
    return rows


def _circles(user):
    return ch.participatable_circles(user)


@login_required
@never_cache
def challenge_list(request):
    mine = list(_circles(request.user))
    rows = list(ch.visible(request.user).prefetch_related("rules"))
    today = timezone.localdate()
    return render(request, "albright_fitness/challenges.html", {
        "challenges": rows, "circles": mine, "today": today,
        "current": [c for c in rows if c.start_date <= today <= c.end_date],
        "upcoming": [c for c in rows if c.start_date > today],
        "past": [c for c in rows if c.end_date < today],
    })


@login_required
@never_cache
def challenge_new(request):
    mine = _circles(request.user)
    if not mine:
        messages.error(request, "Join a circle and turn on Fitness there to start a challenge.")
        return redirect("albright_fitness:challenges")
    if request.method == "POST":
        circle = next((c for c in mine if str(c.pk) == str(request.POST.get("circle"))), None)
        if circle is None:
            messages.error(request, "Pick one of your Fitness circles.")
        else:
            try:
                challenge = ch.create_challenge(
                    request.user, circle, request.POST.get("name"),
                    request.POST.get("start_date"), request.POST.get("end_date"),
                    steps_points=request.POST.get("steps_points"),
                    steps_per=request.POST.get("steps_per"),
                    steps_cap=request.POST.get("steps_cap") or None)
            except ChallengeError as exc:
                messages.error(request, str(exc))
            else:
                messages.success(request, f"Started {challenge.name}.")
                return redirect("albright_fitness:challenge", pk=challenge.pk)
    today = timezone.localdate()
    return render(request, "albright_fitness/challenge_new.html", {
        "circles": mine, "today": today,
        "default_points": ch.DEFAULT_STEPS_POINTS, "default_per": ch.DEFAULT_STEPS_PER,
    })


@login_required
@never_cache
def challenge(request, pk):
    challenge = _challenge(request, pk)
    rules = list(challenge.rules.all())
    entries = (challenge.entries.select_related("user")
               .prefetch_related("comments__user").order_by("-date", "pk")[:60])
    today = timezone.localdate()
    default_day = today if challenge.covers(today) else min(max(today, challenge.start_date), challenge.end_date)
    activities = [(r.activity, r.get_activity_display(), r.unit) for r in rules]
    return render(request, "albright_fitness/challenge.html", {
        "challenge": challenge, "rules": rules, "board": _board(challenge, request.user),
        "entries": entries, "activities": activities or ACTIVITIES,
        "today": today, "default_day": default_day, "can_log": challenge.start_date <= today,
    })


@login_required
@never_cache
def challenge_board(request, pk):
    """JSON {html} of the leaderboard, for the 15s poll on the challenge page (fitness.js)."""
    challenge = _challenge(request, pk)
    html = render_to_string("albright_fitness/_leaderboard.html",
                            {"board": _board(challenge, request.user)}, request=request)
    return JsonResponse({"html": html})


@login_required
@require_POST
def challenge_log(request, pk):
    challenge = _challenge(request, pk)
    try:
        ch.log_entry(challenge, request.user, day=request.POST.get("date"),
                     activity_key=request.POST.get("activity") or ch.STEPS,
                     amount=request.POST.get("amount"), note=request.POST.get("note") or "")
    except ChallengeError as exc:
        messages.error(request, str(exc))
    else:
        messages.success(request, "Logged.")
    return redirect("albright_fitness:challenge", pk=challenge.pk)


@login_required
@require_POST
def challenge_comment(request, pk):
    entry = _entry(request, pk)
    try:
        ch.add_comment(entry, request.user, request.POST.get("text") or "")
    except ChallengeError as exc:
        messages.error(request, str(exc))
    return redirect("albright_fitness:challenge", pk=entry.challenge_id)
