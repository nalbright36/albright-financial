"""Which app (or the Admin area) the current page belongs to - drives the
header's app name (beside the logo), the tab title, app-switch buttons, per-app nav, and accent color
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
APP_LABELS = {app.accent: app.label for app in apps_registry.APPS}         # "Trading" - the header's name
APP_LABELS[apps_registry.HOME_ACCENT] = apps_registry.HOME_LABEL
APPS_BY_ACCENT = {app.accent: app for app in apps_registry.APPS}


def app_key_for(app_name):
    return APP_KEYS.get(app_name, apps_registry.DEFAULT_APP.accent)


def _main_pages(key):
    """(the app's main page, every path that counts as one of its main pages) - the header's Back button goes to
    the first when there's no page of this site to go back to, and hides on the others then."""
    from django.urls import reverse
    if key == apps_registry.HOME_ACCENT:
        return reverse("labs:root"), [reverse("labs:root"), reverse("labs:hub")]
    app = APPS_BY_ACCENT[key]
    paths = [reverse(app.landing_url_name)] + [reverse(url_name) for _key, _label, url_name in app.pages]
    return paths[0], list(dict.fromkeys(paths))


def current_app(request):
    resolved = getattr(request, "resolver_match", None)
    key = app_key_for(resolved.app_name if resolved else None)
    home, mains = _main_pages(key)
    return {
        # For the header's navigation (js/app_nav.js): the app's registry key ("planner"; "home" for Home), its
        # main page and its main pages' paths.
        "current_app_key": APPS_BY_ACCENT[key].key if key in APPS_BY_ACCENT else "home",
        "app_home_url": home,
        "app_main_paths": " ".join(mains),
        "current_app": key,
        "app_brand": APP_BRANDS[key],
        "app_label": APP_LABELS[key],
        "in_reselling_app": key == "reselling",
        "in_opportunities_app": key == "opportunities",
        "in_admin_area": key == "admin",
        "in_labs": key == "labs",
        "in_meals_app": key == "meals",
        "in_notes_app": key == "notes",
        "in_planner_app": key == "planner",
        "in_money_app": key == "money",
        "in_fitness_app": key == "fitness",
        # The app's own CSS/JS (apps_registry.App.stylesheet / script) - on every one of its pages.
        "app_stylesheet": getattr(APPS_BY_ACCENT.get(key), "stylesheet", ""),
        "app_script": getattr(APPS_BY_ACCENT.get(key), "script", ""),
    }
