"""Albright Labs pages.

  "/"        - visitors: the public landing page (no login, no private or market
               data, no API calls; the resume app's strict security headers).
               Signed in: wherever the user's landing setting says, else Home.
  "/home/"   - Home: a tile per app, each with one live number fetched
               separately (tile below), and the "where I land" setting.
  /control/labs/ - Admin > Home page: the landing page's editable text.
"""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from albright_control.access import audit, superuser_required
from albright_resume.security import public_headers

from . import landing, tiles
from .forms import LandingPreferenceForm, LandingSettingsForm
from .models import LandingSettings, UserPreference

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


@login_required
def hub(request):
    pref = UserPreference.for_user(request.user)
    form = LandingPreferenceForm(user=request.user, initial={"landing": pref.landing, "landing_page": pref.landing_page})
    cards = [{"key": key, "label": label, "description": description, "accent": tiles.ACCENTS[key],
              "url_name": url_name}
             for key, (label, description, _fn, superuser_only, url_name) in tiles.TILES.items()
             if not superuser_only or request.user.is_superuser]
    return render(request, "albright_labs/hub.html", {"cards": cards, "landing_form": form, "pref": pref})


@login_required
def tile(request, key):
    """One tile's live number, as JSON - failures become a short note, never a 500."""
    spec = tiles.TILES.get(key)
    if spec is None or (spec[3] and not request.user.is_superuser):
        raise Http404
    try:
        data = spec[2](request.user)
    except tiles.TileUnavailable as exc:
        return JsonResponse({"ok": False, "note": str(exc)})
    except Exception:  # noqa: BLE001 - one broken tile must not break Home
        log.exception("Home tile %s failed", key)
        return JsonResponse({"ok": False, "note": "Not available right now"})
    return JsonResponse({"ok": True, **data})


@login_required
@require_POST
def save_landing(request):
    form = LandingPreferenceForm(request.POST, user=request.user)
    if form.is_valid():
        pref = UserPreference.for_user(request.user)
        pref.landing = form.cleaned_data["landing"]
        pref.landing_page = form.cleaned_data["landing_page"] if pref.landing == "page" else ""
        pref.save()
        messages.success(request, "Saved - that's where you'll land after signing in.")
    else:
        messages.error(request, " ".join(e for errors in form.errors.values() for e in errors))
    return redirect("labs:hub")


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
