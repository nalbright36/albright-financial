"""Which app (or the Admin area) the current page belongs to - drives the
header's brand name, app-switch buttons, per-app nav, and accent color
(templates/index.html sets <body data-app="..."> from it)."""

# URL namespace (app_name) -> the app's key. Anything else - the trading
# app's own pages, login/logout, Django's /admin/ - is "trading". The
# site-wide Admin area (/control/, albright_control) is "admin".
APP_KEYS = {"albright_reselling_app": "reselling", "albright_crypto_app": "opportunities", "albright_control": "admin"}
APP_BRANDS = {"trading": "Albright Trading", "reselling": "Albright Reselling",
              "opportunities": "Albright Opportunities", "admin": "Albright Admin"}


def app_key_for(app_name):
    return APP_KEYS.get(app_name, "trading")


def current_app(request):
    resolved = getattr(request, "resolver_match", None)
    key = app_key_for(resolved.app_name if resolved else None)
    return {
        "current_app": key,
        "app_brand": APP_BRANDS[key],
        "in_reselling_app": key == "reselling",
        "in_opportunities_app": key == "opportunities",
        "in_admin_area": key == "admin",
    }
