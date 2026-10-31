"""`python manage.py check` warns when static files haven't been collected,
or when the collected copy is out of date.

With DEBUG off (production, and this project's default), the site's CSS/JS
are served from STATIC_ROOT - the copy `collectstatic` makes. A file added
since the last collectstatic (e.g. a new app's stylesheet) is a silent 404,
and the browser drops it (nosniff), so the page renders with no styling. That
is exactly what happened to /resume/ - this check names the missing files.

A file CHANGED since the last collectstatic is quieter still: the page links
the new version (cache_bust's ?v= comes from the source file), but the server
sends the old copy. That is how Albright Notes showed Trading's green - the
collected style.css predated Notes' pink body[data-app="notes"] rules, so its
pages fell back to the default (Trading) accent - so this check names those
files too."""
import filecmp
import os

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.checks import Warning, register

HINT = ("Run `python manage.py collectstatic` (then reload the web app) - until then {what}.")


def _listing(paths):
    return ", ".join(sorted(paths)[:8]) + (f" and {len(paths) - 8} more" if len(paths) > 8 else "")


def _stale(source, collected):
    """The collected copy differs from the app's file (checked only when the app's file is newer)."""
    try:
        if os.path.getmtime(source) <= os.path.getmtime(collected):
            return False
        return not filecmp.cmp(source, collected, shallow=False)
    except OSError:
        return False


@register()
def static_files_collected(app_configs=None, **kwargs):
    root = getattr(settings, "STATIC_ROOT", None)
    if settings.DEBUG or not root or not os.path.isdir(root):
        return []   # DEBUG serves from the apps directly; no STATIC_ROOT yet = never deployed from here
    missing, stale, seen = [], [], set()
    for finder in finders.get_finders():
        for path, storage in finder.list(["CVS", ".*", "*~"]):
            if path in seen:
                continue                     # the first finder's copy is the one collectstatic uses
            seen.add(path)
            collected = os.path.join(root, path)
            if not os.path.exists(collected):
                missing.append(path.replace("\\", "/"))
            elif _stale(storage.path(path), collected):
                stale.append(path.replace("\\", "/"))
    warnings = []
    if missing:
        warnings.append(Warning(
            f"{len(missing)} static file(s) aren't in STATIC_ROOT yet: {_listing(missing)}.",
            hint=HINT.format(what="those files 404 and pages that use them render without their CSS/JS"),
            id="albright_labs.W001"))
    if stale:
        warnings.append(Warning(
            f"{len(stale)} static file(s) in STATIC_ROOT are older than the app's copy: {_listing(stale)}.",
            hint=HINT.format(what="the server sends the old versions (e.g. an app's accent color or new styles "
                                  "missing)"),
            id="albright_labs.W002"))
    return warnings
