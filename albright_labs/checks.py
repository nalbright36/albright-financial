"""`python manage.py check` warns when static files haven't been collected.

With DEBUG off (production, and this project's default), the site's CSS/JS
are served from STATIC_ROOT - the copy `collectstatic` makes. A file added
since the last collectstatic (e.g. a new app's stylesheet) is a silent 404,
and the browser drops it (nosniff), so the page renders with no styling. That
is exactly what happened to /resume/ - this check names the missing files."""
import os

from django.conf import settings
from django.contrib.staticfiles import finders
from django.core.checks import Warning, register


@register()
def static_files_collected(app_configs=None, **kwargs):
    root = getattr(settings, "STATIC_ROOT", None)
    if settings.DEBUG or not root or not os.path.isdir(root):
        return []   # DEBUG serves from the apps directly; no STATIC_ROOT yet = never deployed from here
    missing = []
    for finder in finders.get_finders():
        for path, _storage in finder.list(["CVS", ".*", "*~"]):
            if not os.path.exists(os.path.join(root, path)):
                missing.append(path.replace("\\", "/"))
    if not missing:
        return []
    shown = ", ".join(sorted(missing)[:8]) + (f" and {len(missing) - 8} more" if len(missing) > 8 else "")
    return [Warning(
        f"{len(missing)} static file(s) aren't in STATIC_ROOT yet: {shown}.",
        hint="Run `python manage.py collectstatic` (then reload the web app) - until then those files 404 "
             "and pages that use them render without their CSS/JS.",
        id="albright_labs.W001")]
