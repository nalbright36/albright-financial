"""Albright Labs pages.

  "/"        - visitors: the public landing page (no login, no private or market
               data, no API calls; the resume app's strict security headers).
               Signed in: wherever the user's landing setting says, else Home.
  "/home/"   - Home: a tile per app, each with one live number fetched
               separately (tile below), and the "where I land" and
               "show in my navigation" settings.
  "/home/account/" - My account (linked from the header): the same settings.
  /control/labs/ - Admin > Home page: the landing page's editable text.
"""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from albright_control.access import audit, superuser_required
from albright_resume.security import public_headers

from . import app_access, apps_registry, landing, launcher, tiles
from .forms import LandingPreferenceForm, LandingSettingsForm
from .models import LandingSettings

log = logging.getLogger(__name__)


def _blurb(project):
    """A project's one-line blurb for the landing page - never placeholder text."""
    for text in (project.tagline, project.description):
        text = (text or "").strip()
        if text and "[PLACEHOLDER" not in text:
            return text
    return ""


def landing_context():
    """Only published, public resume content - the same things /resume/ shows visitors."""
    from albright_resume.models import Project, ResumeSettings
    site = ResumeSettings.objects.filter(pk=1).first()
    settings_row = LandingSettings.objects.filter(pk=LandingSettings.SINGLETON_PK).first()
    name = (settings_row.name if settings_row and settings_row.name else "") or (site.full_name if site else "")
    projects = [{"title": p.title, "blurb": _blurb(p)}
                for p in Project.objects.filter(published=True).order_by("order", "pk")[:6]]
    return {
        "name": name or "Albright Labs",
        "headline": settings_row.headline if settings_row else "",
        "resume_on": bool(site and site.page_enabled),
        "projects": projects,
    }


@public_headers
def public_landing(request):
    return render(request, "albright_labs/landing.html", landing_context())


@never_cache
def root(request):
    """"/": the public landing for visitors; for a signed-in user, their landing setting."""
    if not request.user.is_authenticated:
        return public_landing(request)
    target = landing.landing_url(request)
    if target:
        return redirect(target)
    return hub(request)


def _settings_context(request):
    """The "where I land" and "show in my navigation" forms (Home and My account)."""
    pref = app_access.preference(request)
    form = LandingPreferenceForm(user=request.user, pref=pref,
                                 initial={"landing": pref.landing, "landing_page": pref.landing_page})
    shown = set(app_access.nav_keys(request.user, pref))
    nav_rows = [{"key": key, "label": apps_registry.BY_KEY[key].label, "accent": apps_registry.BY_KEY[key].accent,
                 "color": apps_registry.BY_KEY[key].color, "shown": key in shown}
                for key in app_access.ordered_keys(request.user, pref)]
    return {"landing_form": form, "pref": pref, "nav_rows": nav_rows}


def _back(request, default):
    """Where a settings form came from (Home or My account) - only a page on this site."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                  require_https=request.is_secure()):
        return redirect(target)
    return redirect(default)


@login_required
def hub(request):
    pref = app_access.preference(request)
    cards = [{"key": app.key, "label": app.short_label, "description": app.description, "accent": app.accent,
              "color": app.color, "url_name": app.landing_url_name, "tile": bool(app.tile)}
             for app in (apps_registry.BY_KEY[key] for key in app_access.nav_keys(request.user, pref))]
    return render(request, "albright_labs/hub.html", {"cards": cards, **_settings_context(request)})


@login_required
def account(request):
    """My account: which apps show in my navigation (and their order), and where I land."""
    return render(request, "albright_labs/account.html", _settings_context(request))


@login_required
def tile(request, key):
    """One tile's live number, as JSON - failures become a short note, never a 500.
    404 for an app the user can't open (hidden ones still answer)."""
    app = apps_registry.BY_KEY.get(key)
    if app is None or not app.tile or not app_access.can_use(request.user, app_access.preference(request), key):
        raise Http404
    try:
        data = getattr(tiles, app.tile)(request.user)
    except tiles.TileUnavailable as exc:
        return JsonResponse({"ok": False, "note": str(exc)})
    except Exception:  # noqa: BLE001 - one broken tile must not break Home
        log.exception("Home tile %s failed", key)
        return JsonResponse({"ok": False, "note": "Not available right now"})
    return JsonResponse({"ok": True, **data})


@login_required
@never_cache
def launcher_lines(request):
    """The header launcher's live lines, as JSON: {"lines": {app key: text or null}} for
    the tiles in this user's launcher (albright_labs.launcher - cached 60s per user)."""
    return JsonResponse({"lines": launcher.lines(request.user, app_access.preference(request))})


@login_required
@require_POST
def save_landing(request):
    pref = app_access.preference(request)
    form = LandingPreferenceForm(request.POST, user=request.user, pref=pref)
    if form.is_valid():
        pref.landing = form.cleaned_data["landing"]
        pref.landing_page = form.cleaned_data["landing_page"] if pref.landing == "page" else ""
        pref.save()
        messages.success(request, "Saved - that's where you'll land after signing in.")
    else:
        messages.error(request, " ".join(e for errors in form.errors.values() for e in errors))
    return _back(request, "labs:hub")


@login_required
@require_POST
def save_nav(request):
    """"Show in my navigation" and the order. Without JS the up/down buttons submit
    here too (move=up:trade) and the order changes at once."""
    pref = app_access.preference(request)
    order = request.POST.getlist("order")
    move = request.POST.get("move", "")
    direction, _, key = move.partition(":")
    if key in order and direction in ("up", "down"):
        i = order.index(key)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
    try:
        pref.visible_apps, pref.app_order = app_access.clean_nav(request.user, pref, request.POST.getlist("visible"),
                                                                 order)
    except ValueError as exc:
        messages.error(request, str(exc))
        return _back(request, "labs:hub")
    pref.save()
    app_access.forget(request)
    if not move:
        messages.success(request, "Saved - your navigation is updated.")
    return _back(request, "labs:hub")


@login_required
@require_POST
def show_app(request, key):
    """The "Hidden from your nav · Show it" note: put this app back in the nav."""
    pref = app_access.preference(request)
    app = apps_registry.BY_KEY.get(key)
    if app is None or not app_access.can_use(request.user, pref, key):
        raise Http404
    if key not in (pref.visible_apps or []):
        pref.visible_apps = list(pref.visible_apps or []) + [key]
        pref.save()
    messages.success(request, f"{app.label} is back in your navigation.")
    return _back(request, app.landing_url_name)


@superuser_required
def landing_settings(request):
    """Admin > Home page: the public landing page's name and headline."""
    row = LandingSettings.load()
    form = LandingSettingsForm(request.POST or None, instance=row)
    if request.method == "POST" and form.is_valid():
        form.save()
        audit(request, "landing_settings_saved", row.headline[:100])
        messages.success(request, "Landing page saved.")
        return redirect("labs_admin:settings")
    return render(request, "albright_labs/landing_settings.html", {"form": form, **landing_context()})
