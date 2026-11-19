"""Which app (or the Admin area, Home or your account) the current page belongs to - drives the header's name
(beside the logo), the tab title, app-switch buttons, per-app nav, and accent color (templates/index.html sets
<body data-app="..."> from it).

  - An app's pages: that app - by URL namespace, or (the trading app has none) by the package its view lives in.
  - The site-wide Admin area (/control/, albright_control) and Admin > Invite requests (labs_admin): "admin".
  - Account-level pages (apps_registry.ACCOUNT_PAGES: My account, API keys, Your AI assistant, Circles, People,
    Notifications): "account" - a neutral "Account" chip, the Labs accent and the Account sub-nav.
  - Everything else - Home ("/", "/home/"), sign-in, Django's /admin/, the Bot API pages: "labs" (Home). No app is
    ever a default.
"""
from albright_labs import apps_registry, archive

ACCOUNT = apps_registry.ACCOUNT_KEY
APP_KEYS = {ns: app.accent for ns, app in apps_registry.BY_NAMESPACE.items() if ns}
APP_KEYS[apps_registry.HOME_NAMESPACE] = apps_registry.HOME_ACCENT
APP_BRANDS = {app.accent: app.brand for app in apps_registry.APPS}
APP_BRANDS[apps_registry.HOME_ACCENT] = apps_registry.HOME_BRAND
APP_BRANDS[ACCOUNT] = apps_registry.ACCOUNT_BRAND
APP_LABELS = {app.accent: app.label for app in apps_registry.APPS}         # "Trading" - the header's name
APP_LABELS[apps_registry.HOME_ACCENT] = apps_registry.HOME_LABEL
APP_LABELS[ACCOUNT] = apps_registry.ACCOUNT_LABEL
APPS_BY_ACCENT = {app.accent: app for app in apps_registry.APPS}


def app_key_for(app_name, match=None):
    """The header key for a page: an app's accent, "admin", "account" or "labs" (Home) - never a default app."""
    if match is not None and apps_registry.is_account_page(match):
        return ACCOUNT
    if app_name in APP_KEYS:
        return APP_KEYS[app_name]
    app = archive.app_for_view(match) if match is not None else None
    return app.accent if app is not None else apps_registry.HOME_ACCENT


def _main_pages(key):
    """(the app's main page, every path that counts as one of its main pages) - the header's Back button goes to
    the first when there's no page of this site to go back to, and hides on the others then."""
    from django.urls import reverse
    if key == apps_registry.HOME_ACCENT:
        return reverse("labs:root"), [reverse("labs:root"), reverse("labs:hub")]
    if key == ACCOUNT:
        return reverse("labs:account"), [reverse("labs:account")]
    app = APPS_BY_ACCENT[key]
    paths = [reverse(app.landing_url_name)] + [reverse(url_name) for _key, _label, url_name in app.pages]
    return paths[0], list(dict.fromkeys(paths))


def current_app(request):
    resolved = getattr(request, "resolver_match", None)
    key = app_key_for(resolved.app_name if resolved else None, resolved)
    home, mains = _main_pages(key)
    return {
        # For the header's navigation (js/app_nav.js): the app's registry key ("planner"; "home" for Home), its
        # main page and its main pages' paths.
        "current_app_key": APPS_BY_ACCENT[key].key if key in APPS_BY_ACCENT else (ACCOUNT if key == ACCOUNT else "home"),
        "app_home_url": home,
        "app_main_paths": " ".join(mains),
        "current_app": key,
        "app_brand": APP_BRANDS[key],
        "app_label": APP_LABELS[key],
        "in_reselling_app": key == "reselling",
        "in_opportunities_app": key == "opportunities",
        "in_admin_area": key == "admin",
        "in_labs": key == "labs",
        "in_account": key == ACCOUNT,
        "in_meals_app": key == "meals",
        "in_notes_app": key == "notes",
        "in_planner_app": key == "planner",
        "in_money_app": key == "money",
        "in_fitness_app": key == "fitness",
        "in_weather_app": key == "weather",
        # The app's own CSS/JS (apps_registry.App.stylesheet / script) - on every one of its pages.
        "app_stylesheet": getattr(APPS_BY_ACCENT.get(key), "stylesheet", ""),
        "app_script": getattr(APPS_BY_ACCENT.get(key), "script", ""),
    }
