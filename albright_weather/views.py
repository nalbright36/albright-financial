"""Albright Weather (/weather/): current conditions, details, alerts, an hourly
chart, a 16-day outlook, saved places, and a radar / tropics teaser.

show_weather on PlannerSettings only hides forecasts on the Planner. This app
always shows. A down API never fails the page.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from . import services
from .models import WeatherPlace


def _now_display(now):
    """The viewer’s local time as a string, not converted to TIME_ZONE."""
    if now is None:
        return ""
    try:
        return now.strftime("%a, %b %-d, %Y, %-I:%M %p %Z")
    except ValueError:
        return now.strftime("%a, %b %d, %Y, %I:%M %p %Z").replace(" 0", " ")


@login_required
@require_GET
def home(request):
    chips, chosen = services.chips_for(request.user, request.GET.get("place") or "")
    bundle = {
        "current": None, "details": None, "hours": [], "chart": "",
        "outlook": [], "alerts": [], "radar": services.radar_links(None, None),
        "tropics": services.tropics_links(), "now": None, "tz": "", "offline": False,
    }
    if chosen is not None:
        try:
            bundle = services.load_place(chosen.lat, chosen.lon, request.user)
        except Exception:
            bundle["offline"] = True
        bundle["radar"] = services.radar_links(chosen.lat, chosen.lon)
        bundle["tropics"] = services.tropics_links()
    return render(request, "albright_weather/home.html", {
        "chips": chips,
        "chosen": chosen,
        "current": bundle["current"],
        "details": bundle["details"],
        "hours": bundle["hours"],
        "chart": bundle["chart"],
        "outlook": bundle["outlook"],
        "alerts": bundle["alerts"],
        "radar": bundle["radar"],
        "tropics": bundle["tropics"],
        "now": bundle.get("now"),
        "now_display": _now_display(bundle.get("now")),
        "offline": bundle.get("offline"),
    })


@login_required
@require_POST
def place_add(request):
    place, error = services.add_place(request.user, request.POST.get("q") or request.POST.get("query") or "")
    if error:
        messages.error(request, error)
        return redirect(reverse("albright_weather:home"))
    return redirect(f"{reverse('albright_weather:home')}?place=p{place.pk}")


@login_required
@require_POST
def place_remove(request, pk):
    place = get_object_or_404(WeatherPlace, pk=pk, user=request.user)
    place.delete()
    return redirect(reverse("albright_weather:home"))
