"""The site's app icons outside /static/: the web app manifest (/site.webmanifest) and the two files browsers
and iOS ask for at the root (/favicon.ico, /apple-touch-icon.png). The icons themselves live in
albright_trading_app/static/icons/; templates/_head_icons.html links them on every page. No login needed."""
import json

from django.contrib.staticfiles import finders
from django.contrib.staticfiles.storage import staticfiles_storage
from django.http import FileResponse, Http404, HttpResponse
from django.templatetags.static import static
from django.views.decorators.cache import cache_control
from django.views.decorators.http import require_GET

THEME_COLOR = "#0b0d10"
CACHE = 60 * 60 * 24      # a day: a new icon shows up by tomorrow without any cache busting


def _icon_file(name):
    """The icon's file: from the app's static folder (dev), else the collected copy (production)."""
    path = finders.find(f"icons/{name}")
    if path:
        return open(path, "rb")
    try:
        return staticfiles_storage.open(f"icons/{name}")
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise Http404 from exc


def _serve(name, content_type):
    @require_GET
    @cache_control(public=True, max_age=CACHE)
    def view(request):
        return FileResponse(_icon_file(name), content_type=content_type)
    return view


favicon = _serve("favicon.ico", "image/x-icon")
apple_touch_icon = _serve("apple-touch-icon.png", "image/png")


@require_GET
@cache_control(public=True, max_age=CACHE)
def manifest(request):
    data = {
        "name": "Albright Labs",
        "short_name": "Albright",
        "start_url": "/",
        "display": "standalone",
        "background_color": THEME_COLOR,
        "theme_color": THEME_COLOR,
        "icons": [
            {"src": static("icons/icon-192.png"), "sizes": "192x192", "type": "image/png"},
            {"src": static("icons/icon-512.png"), "sizes": "512x512", "type": "image/png"},
            {"src": static("icons/icon-maskable-512.png"), "sizes": "512x512", "type": "image/png",
             "purpose": "maskable"},
        ],
    }
    return HttpResponse(json.dumps(data, indent=2), content_type="application/manifest+json")
