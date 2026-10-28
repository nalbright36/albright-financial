"""Which app (or the Admin area) the current page belongs to - drives the
header's brand name, app-switch buttons, per-app nav, and accent color
(templates/index.html sets <body data-app="..."> from it)."""
from albright_labs import apps_registry

# URL namespace (app_name) -> the app's accent key, from the app registry
# (albright_labs.apps_registry). Anything else - the trading app's own pages,
# login/logout, Django's /admin/ - is "trading". The site-wide Admin area
# (/control/, albright_control) is "admin", and so are the resume management
# pages (/control/resume/, albright_resume_admin) and Admin > Home page
# (labs_admin). The hub ("/", "/home/") is "labs".
APP_KEYS = {ns: app.accent for ns, app in apps_registry.BY_NAMESPACE.items() if ns}
APP_KEYS[apps_registry.HOME_NAMESPACE] = apps_registry.HOME_ACCENT
APP_BRANDS = {app.accent: app.brand for app in apps_registry.APPS}
APP_BRANDS[apps_registry.HOME_ACCENT] = apps_registry.HOME_BRAND


def app_key_for(app_name):
    return APP_KEYS.get(app_name, apps_registry.DEFAULT_APP.accent)


def current_app(request):
    resolved = getattr(request, "resolver_match", None)
    key = app_key_for(resolved.app_name if resolved else None)
    return {
        "current_app": key,
        "app_brand": APP_BRANDS[key],
        "in_reselling_app": key == "reselling",
        "in_opportunities_app": key == "opportunities",
        "in_admin_area": key == "admin",
        "in_labs": key == "labs",
    }
