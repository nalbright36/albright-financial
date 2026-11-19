"""Archived apps (settings.ARCHIVED_APPS - docs/ARCHIVED_APPS.md): an app that is switched off but kept whole, so it
can be restored by taking its slug out of the setting and redeploying.

Archiving changes nothing in the database - no tables dropped, no rows deleted, no migrations removed - and the app
stays in INSTALLED_APPS with all its code. What changes while it's archived:

  - Hidden everywhere: app_access.allowed_keys() leaves it out, so the launcher, Home, the nav, "where I land",
    Admin's per-user app list, the landing page, sharing and the Bot API (scopes.allowed) never offer it. People keep
    their stored allowed-app entries, so restoring brings their access back.
  - Its pages (AppAccessMiddleware -> blocked_response): a plain "This app is archived" page with HTTP 404 for
    everyone - signed in or not - except superusers, who still open it with an "Archived app" banner
    (context_processors.archived_banner) to look at old data.
  - Background work: each of its management commands is wrapped in @skip_if_archived and exits with
    "app archived, skipping" before doing anything (no API calls, no Telegram, no stored secret is decrypted).

is_app_archived(slug) takes any of an app's names: its registry key ("trade"), accent ("trading"), label
("Trading"), last_app cookie value or Python package ("albright_trading_app").
"""
import functools

from django.conf import settings

from . import apps_registry

SKIP_MESSAGE = "app archived, skipping"


def _names(app):
    return {app.key, app.accent, app.label.lower(), app.last_app_cookie, app.package}


def _find(slug):
    slug = str(slug or "").strip().lower()
    return next((app for app in apps_registry.APPS if slug and slug in _names(app)), None)


def archived_keys():
    """Registry keys of the archived apps (Admin can't be archived)."""
    wanted = {str(s).strip().lower() for s in getattr(settings, "ARCHIVED_APPS", None) or []}
    return frozenset(app.key for app in apps_registry.APPS if not app.superuser_only and _names(app) & wanted)


def is_app_archived(slug):
    """Whether this app (any of its names - see the module docstring) is archived. Unknown names: False."""
    app = _find(slug)
    return app is not None and app.key in archived_keys()


def live_keys(keys):
    """These registry keys minus the archived ones, order kept."""
    archived = archived_keys()
    return [key for key in keys if key not in archived]


def live_apps():
    """The registry's apps that aren't archived, in registry order."""
    archived = archived_keys()
    return [app for app in apps_registry.APPS if app.key not in archived]


def app_for_view(match):
    """The registry app a resolved view (a ResolverMatch) belongs to - by its URL namespace, or else by the Python
    package its view lives in (the trading app's pages have no namespace) - or None. Ungated pages (sign-in, password
    reset) belong to no app."""
    if match is None:
        return None
    app = apps_registry.BY_NAMESPACE.get(match.app_name) if match.app_name else None
    if app is None:
        module = getattr(match.func, "__module__", "") or ""
        app = next((a for a in apps_registry.APPS if a.package and (module == a.package
                                                                    or module.startswith(a.package + "."))), None)
    if app is None or match.url_name in app.ungated_url_names:
        return None
    return app


def archived_app_for(request):
    """The archived app this request's view belongs to, or None."""
    app = app_for_view(getattr(request, "resolver_match", None))
    return app if app is not None and app.key in archived_keys() else None


def blocked_response(request, app, status=404, needs_secrets=False):
    """The plain "This app is archived" page (the site's error-page look) - HTTP 404, as if it weren't there; JSON for
    AJAX/API callers. For a superuser whose archived page needed the app's saved credentials (needs_secrets): the same
    page, 200, saying why it can't show."""
    from albright_trading_proj.errors import render_error
    if needs_secrets:
        sentence = (f"{app.label} is archived, so its saved credentials aren't used - this page needs them. Its stored "
                    "data is untouched; pages that only read it still open.")
    else:
        sentence = f"{app.label} has been switched off for now. Nothing in it was deleted."
    return render_error(request, 404, template="albright_labs/archived_app.html", http_status=status,
                        title="This app is archived", sentence=sentence)


class ArchivedAppError(RuntimeError):
    """An archived app's stored secret (say its Alpaca keys) was about to be used."""

    def __init__(self, app):
        self.app = app
        super().__init__(f"{app.label} is archived - its saved credentials aren't used.")


def refuse_if_archived(slug):
    """Call right before decrypting or using an app's stored secret: raises ArchivedAppError while it's archived."""
    if is_app_archived(slug):
        raise ArchivedAppError(_find(slug))


def part_of(slug):
    """View decorator for a page or action of an app that lives outside its package (say the Grok Bot page's note
    buttons, in albright_bot_api): blocked exactly like the app's own pages while it's archived."""
    def decorate(view):
        @functools.wraps(view)
        def wrapper(request, *args, **kwargs):
            if is_app_archived(slug):
                user = getattr(request, "user", None)
                if not (user is not None and user.is_authenticated and user.is_active and user.is_superuser):
                    return blocked_response(request, _find(slug))
            return view(request, *args, **kwargs)
        return wrapper
    return decorate


def skip_if_archived(slug):
    """Class decorator for a management command of an archived-able app: while the app is archived, handle() writes
    "app archived, skipping" and returns at once - before any API call, alert or secret is touched."""
    def decorate(command_class):
        handle = command_class.handle

        @functools.wraps(handle)
        def guarded(self, *args, **options):
            if is_app_archived(slug):
                self.stdout.write(SKIP_MESSAGE)
                return None
            return handle(self, *args, **options)

        command_class.handle = guarded
        command_class.archived_app_slug = slug
        return command_class
    return decorate
