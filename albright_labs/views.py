"""Albright Labs pages.

  "/"        - visitors: the public landing page (no login, no private or market
               data, no API calls; the resume app's strict security headers).
               Signed in: their "Open after sign-in" app if set, else Home (the
               app picker). "/?picker=1" always shows the picker (labs.landing).
               Someone who can open just one app goes straight to it.
  "/home/"   - Home, the app picker: a greeting and a tile per app (allowed and
               shown, in their order, Favorites first), each with one live line
               from the launcher's endpoint (launcher_lines). Its Edit mode
               reorders, pins, hides/shows, and sets "Open after sign-in" and
               the Today panel (save_picker, toggle_pin).
               Someone who can't open any app yet (a new account) sees only
               "access pending" there.
  "/home/account/" - My account (linked from the header): the same settings as forms.
  "/join/<token>/" - an invite's sign-up page (invite_views).
  /control/labs/ - Admin > Home page: the landing page's editable text.
"""
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from albright_control.access import audit, superuser_required
from albright_resume.security import public_headers

from . import app_access, apps_registry, landing, launcher, tiles
from .forms import LandingSettingsForm
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
    """"/": the public landing for visitors. Signed in: "/?picker=1" is always the picker; otherwise their
    "Open after sign-in" app, else the picker. Someone with a single app goes straight to it either way."""
    if not request.user.is_authenticated:
        return public_landing(request)
    target = landing.single_app_url(request)
    if not target and request.GET.get(landing.PICKER_PARAM) != "1":
        target = landing.landing_url(request)
    if target:
        return redirect(target)
    return hub(request)


def _settings_context(request):
    """The "show in my navigation" and "Open after sign-in" settings (My account; Home's Edit mode)."""
    pref = app_access.preference(request)
    shown = set(app_access.nav_keys(request.user, pref))
    nav_rows = [{"key": key, "label": apps_registry.BY_KEY[key].label, "accent": apps_registry.BY_KEY[key].accent,
                 "color": apps_registry.BY_KEY[key].color, "shown": key in shown}
                for key in app_access.ordered_keys(request.user, pref)]
    choices = landing.open_after_choices(request.user, pref)
    return {"pref": pref, "nav_rows": nav_rows,
            "open_after_plain": [c for c in choices if isinstance(c[1], str)],
            "open_after_groups": [c for c in choices if not isinstance(c[1], str)],
            "open_after": landing.open_after_value(pref),
            "today_setting": app_access.can_use(request.user, pref, "planner")}


def _back(request, default):
    """Where a settings form came from (Home or My account) - only a page on this site."""
    target = request.POST.get("next", "")
    if target and url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()},
                                                  require_https=request.is_secure()):
        return redirect(target)
    return redirect(default)


def greeting(user, now=None):
    """"Good morning, Nathan" - by the site's local time; the first name, else the username."""
    hour = timezone.localtime(now).hour
    part = "morning" if 4 <= hour < 12 else ("afternoon" if 12 <= hour < 18 else "evening")
    return f"Good {part}, {(user.first_name or user.get_username()).strip()}"


def picker_tiles(user, pref):
    """Every app this user can open, in their order with Admin last: (favorites, the rest). Each tile says if it's
    shown (hidden ones appear only in Edit mode) and pinned."""
    shown, pinned = set(app_access.nav_keys(user, pref)), set(pref.pinned_apps or [])
    keys = app_access.ordered_keys(user, pref)
    keys = [k for k in keys if k != "admin"] + [k for k in keys if k == "admin"]
    tiles = [{"key": key, "label": app.label, "accent": app.accent, "color": app.color,
              "url_name": app.landing_url_name, "shown": key in shown, "pinned": key in pinned}
             for key, app in ((key, apps_registry.BY_KEY[key]) for key in keys)]
    return [t for t in tiles if t["pinned"]], [t for t in tiles if not t["pinned"]]


@login_required
@never_cache
def hub(request):
    """Home, the app picker (always - a default app only applies to "/" without ?picker=1)."""
    pref = app_access.preference(request)
    if not app_access.allowed_keys(request.user, pref):
        # A new account before any app is granted: nothing to show but that.
        return render(request, "albright_labs/pending.html")
    single = landing.single_app_url(request)
    if single:
        return redirect(single)
    favorites, others = picker_tiles(request.user, pref)
    return render(request, "albright_labs/hub.html", {
        "greeting": greeting(request.user), "favorites": favorites, "others": others,
        "editing": request.GET.get("edit") == "1", "home_url": landing.home_url(),
        "planner_today": _planner_today(request, pref) if pref.show_today_panel else None,
        **_settings_context(request)})


def _planner_today(request, pref):
    """Home's Today panel (only with "Show Today panel" on), for someone who can open the Planner (else None). A
    failure hides the panel."""
    if not app_access.can_use(request.user, pref, "planner"):
        return None
    try:
        from albright_planner.planner import today_panel
        return {**today_panel(request.user, show_completed=pref.show_completed_today), "here": request.get_full_path()}
    except Exception:  # noqa: BLE001 - the panel must never break Home
        log.exception("Home's Today panel failed")
        return None


@login_required
def account(request):
    """My account: which apps show in my navigation (and their order), and where I land - and a link to My API
    keys for someone allowed them."""
    from albright_bot_api.scopes import keys_allowed
    return render(request, "albright_labs/account.html", {**_settings_context(request),
                                                          "api_keys": keys_allowed(request.user)})


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


def _set_open_after(request, pref, reset=False):
    """Apply the posted "Open after sign-in" choice (and the Today panel switch, when its form has one). False if
    the choice isn't available: the saved one stays - or, with reset (Edit mode, where its app may just have been
    hidden), it goes back to the picker."""
    if "today_panel" in request.POST:
        pref.show_today_panel = request.POST.get("show_today") == "1"
        pref.show_completed_today = request.POST.get("show_completed") == "1"
    try:
        pref.landing, pref.landing_page = landing.parse_open_after(request.user, request.POST.get("open_after"), pref)
        return True
    except ValueError:
        if reset:
            pref.landing, pref.landing_page = landing.HOME, ""
        return False


@login_required
@require_POST
def save_landing(request):
    """My account: "Open after sign-in" and "Show Today panel"."""
    pref = app_access.preference(request)
    if _set_open_after(request, pref):
        messages.success(request, "Saved - that's what opens after you sign in.")
    else:
        messages.error(request, "That choice isn't available.")
    pref.save()
    return _back(request, "labs:account")


def _apply_move(order, move):
    direction, _, key = (move or "").partition(":")
    if key in order and direction in ("up", "down"):
        i = order.index(key)
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
    return order


@login_required
@require_POST
def save_picker(request):
    """Home's Edit mode, saved by Done: the order (tiles as arranged, Favorites first), which apps show, "Open
    after sign-in" and the Today panel. Without JS the up/down buttons submit here too (move=up:<key>) and come
    back to Edit mode."""
    pref = app_access.preference(request)
    move = request.POST.get("move", "")
    order = _apply_move(request.POST.getlist("order"), move)
    try:
        pref.visible_apps, pref.app_order = app_access.clean_nav(request.user, pref, request.POST.getlist("visible"),
                                                                 order)
    except ValueError as exc:
        messages.error(request, str(exc))
        return redirect(landing.home_url() + "&edit=1")
    ok = _set_open_after(request, pref, reset=True)
    pref.save()
    app_access.forget(request)
    if move:
        return redirect(landing.home_url() + "&edit=1")
    if ok:
        messages.success(request, "Saved.")
    else:
        messages.warning(request, "Saved - that app is hidden now, so the app picker opens after you sign in.")
    return redirect(landing.home_url())


@login_required
@require_POST
def toggle_pin(request):
    """A tile's pin: in or out of Home's Favorites row. JSON for hub.js; a plain post comes back to Home."""
    pref = app_access.preference(request)
    key = request.POST.get("key", "")
    if not app_access.can_use(request.user, pref, key):
        raise Http404
    now_pinned = key not in (pref.pinned_apps or [])
    pref.pinned_apps = [k for k in (pref.pinned_apps or []) if k != key] + ([key] if now_pinned else [])
    pref.save()
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return JsonResponse({"ok": True, "key": key, "pinned": now_pinned})
    return redirect(landing.home_url() + ("&edit=1" if request.POST.get("editing") == "1" else ""))


@login_required
@require_POST
def save_nav(request):
    """"Show in my navigation" and the order. Without JS the up/down buttons submit
    here too (move=up:trade) and the order changes at once."""
    pref = app_access.preference(request)
    move = request.POST.get("move", "")
    order = _apply_move(request.POST.getlist("order"), move)
    try:
        pref.visible_apps, pref.app_order = app_access.clean_nav(request.user, pref, request.POST.getlist("visible"),
                                                                 order)
    except ValueError as exc:
        messages.error(request, str(exc))
        return _back(request, "labs:account")
    pref.save()
    app_access.forget(request)
    if not move:
        messages.success(request, "Saved - your navigation is updated.")
    return _back(request, "labs:account")


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
