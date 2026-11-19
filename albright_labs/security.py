"""Strict response headers for the public pages: the home page for visitors and its footer pages (which also
load Google Fonts - its stylesheet @imports them, the way the app's style.css does)."""
from functools import wraps

CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "manifest-src 'self'; connect-src 'self'; worker-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")


FONTS_CSP = (CSP.replace("style-src 'self'", "style-src 'self' https://fonts.googleapis.com")
             .replace("font-src 'self'", "font-src 'self' https://fonts.gstatic.com"))


def _with_headers(view, csp):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        response = view(request, *args, **kwargs)
        response.setdefault("Content-Security-Policy", csp)
        response.setdefault("X-Content-Type-Options", "nosniff")
        response.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        return response
    return wrapper


def public_headers(view):
    return _with_headers(view, CSP)


def landing_headers(view):
    """public_headers, plus Google Fonts (style-src for the CSS, font-src for the files)."""
    return _with_headers(view, FONTS_CSP)
