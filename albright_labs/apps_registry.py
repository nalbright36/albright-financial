"""The one list of apps on the site. The header's app launcher, Home's
launcher tiles, the "where I land" dropdown, the last_app cookie, the header
brand/accent and the access checks (albright_labs.access) all read from it -
adding an app (e.g. "Meals") is one more App(...) below, plus its URLs.

Home (the Labs hub, "/home/") isn't an app here: it's always shown and open
to every signed-in user. Admin is, but superuser_only - it can't be granted,
superusers always have it, and its views already 404 for everyone else.

This module imports nothing from Django models, so settings, middleware and
migrations can all import it.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class App:
    key: str                  # stored in UserPreference.allowed_apps / visible_apps / app_order
    label: str                # "Trading" - Admin > Users, My account
    short_label: str          # "Trade" - the header launcher's tile and Home's tile
    brand: str                # "Albright Trading" - the header brand and tab title
    accent: str               # <body data-app>, the launcher tile's data-app-target and Home tile's accent
    color: str                # the app's own color, used if style.css has no --app-<accent> for it
    landing_url_name: str     # where its launcher tile and Home tile go (never "/")
    namespaces: tuple         # URL namespaces (app_name) whose views belong to it; "" = no namespace
    description: str          # Home tile
    last_app_cookie: str      # the last_app cookie's value for it (albright_trading_proj.middleware)
    tile: str = ""            # the function in albright_labs.tiles for Home's live number
    pages: tuple = ()         # ((key, label, URL name), ...) - "Where I land" options
    superuser_only: bool = False
    # The app's own stylesheet / script (static paths): templates/index.html includes them on EVERY page of the
    # app (context_processors.current_app), so no page can miss its styles.
    stylesheet: str = ""
    script: str = ""
    # Pages in its namespace that stay open to anyone (sign in, password reset).
    ungated_url_names: frozenset = field(default_factory=frozenset)


APPS = (
    App(key="trade", label="Trading", short_label="Trade", brand="Albright Trading", accent="trading",
        color="#38BDF8", landing_url_name="trading_home", namespaces=("",),
        description="Algorithmic trading on Alpaca - strategies, positions, the Grok bot.",
        last_app_cookie="trading", tile="trade",
        pages=(("trade:home", "Home", "trading_home"),
               ("trade:account", "Trading Account", "trading_account"),
               ("trade:grok_bot", "Grok Bot", "grok_bot"),
               ("trade:scanner", "Market Scanner", "market_scanner"),
               ("trade:strategies", "Strategies", "strategies")),
        # The trading URLconf (no namespace) also holds the site's sign-in pages (no public
        # registration - accounts are invite-only, albright_labs.invites), and
        # the old /crypto/ prefixes' redirect (the Opportunities check applies where it lands).
        ungated_url_names=frozenset({"user_login", "user_logout", "opportunities_legacy_redirect", "password_reset", "password_reset_done",
                                     "password_reset_confirm", "password_reset_complete"})),
    App(key="resell", label="Reselling", short_label="Resell", brand="Albright Reselling", accent="reselling",
        color="#60A5FA", landing_url_name="albright_reselling_app:dashboard", namespaces=("albright_reselling_app",),
        description="Auction scanning, the watchlist, and the resale ledger.",
        last_app_cookie="reseller", tile="resell",
        pages=(("resell:today", "Today", "albright_reselling_app:today"),
               ("resell:dashboard", "Dashboard", "albright_reselling_app:dashboard"),
               ("resell:watchlist", "Watchlist", "albright_reselling_app:watchlist"),
               ("resell:review", "Review", "albright_reselling_app:review_queue"),
               ("resell:ledger", "Ledger", "albright_reselling_app:ledger"),
               ("resell:calculator", "Calculator", "albright_reselling_app:calculator"))),
    App(key="opps", label="Opportunities", short_label="Opps", brand="Albright Opportunities",
        accent="opportunities", color="#A78BFA", landing_url_name="albright_crypto_app:inbox",
        namespaces=("albright_crypto_app",),
        description="Jobs, freelance work, bounties and crypto - triaged in one Inbox.",
        last_app_cookie="opportunities", tile="opps",
        pages=(("opps:inbox", "Inbox", "albright_crypto_app:inbox"),
               ("opps:pipeline", "Pipeline", "albright_crypto_app:pipeline"),
               ("opps:earnings", "Earnings", "albright_crypto_app:earnings"))),
    App(key="meals", label="Meals", short_label="Meals", brand="Albright Meals", accent="meals",
        color="#F97316", landing_url_name="albright_meals:week", namespaces=("albright_meals",),
        description="This week's meals, the shared grocery list, meal ideas and recipes.",
        last_app_cookie="meals", tile="meals",
        pages=(("meals:week", "This week", "albright_meals:week"),
               ("meals:groceries", "Groceries", "albright_meals:groceries"),
               ("meals:ideas", "Ideas", "albright_meals:ideas"),
               ("meals:recipes", "Recipes", "albright_meals:recipes"))),
    App(key="notes", label="Notes", short_label="Notes", brand="Albright Notes", accent="notes",
        color="#EC4899", landing_url_name="albright_notes:list", namespaces=("albright_notes",),
        description="Private Markdown notes and checklists - share one, link it to a recipe, lot or strategy.",
        last_app_cookie="notes", tile="notes",
        pages=(("notes:list", "Notes", "albright_notes:list"),),
        stylesheet="albright_notes/notes.css", script="albright_notes/notes.js"),
    App(key="planner", label="Planner", short_label="Plan", brand="Albright Planner", accent="planner",
        color="#84CC16", landing_url_name="albright_planner:home", namespaces=("albright_planner",),
        description="Tasks and events - repeating items, check-off, and Day, Week and Month views.",
        last_app_cookie="planner", tile="planner",
        pages=(("planner:day", "Day", "albright_planner:day"), ("planner:week", "Week", "albright_planner:week"),
               ("planner:month", "Month", "albright_planner:month")),
        stylesheet="albright_planner/planner.css", script="albright_planner/planner.js"),
    App(key="money", label="Money", short_label="Money", brand="Albright Money", accent="money",
        color="#34D399", landing_url_name="albright_money:dashboard", namespaces=("albright_money",),
        description="Accounts, transactions, monthly budgets and bank CSV import - yours and your circles'.",
        last_app_cookie="money", tile="money",
        pages=(("money:dashboard", "Dashboard", "albright_money:dashboard"),
               ("money:transactions", "Transactions", "albright_money:transactions"),
               ("money:budget", "Budget", "albright_money:budget"),
               ("money:accounts", "Accounts", "albright_money:accounts")),
        stylesheet="albright_money/money.css", script="albright_money/money.js"),
    App(key="fitness", label="Fitness", short_label="Fitness", brand="Albright Fitness", accent="fitness",
        color="#FACC15", landing_url_name="albright_fitness:dashboard", namespaces=("albright_fitness",),
        description="Log workouts at the gym, track personal records, and follow a program on your Planner.",
        last_app_cookie="fitness", tile="fitness",
        pages=(("fitness:dashboard", "Dashboard", "albright_fitness:dashboard"),
               ("fitness:challenges", "Challenges", "albright_fitness:challenges"),
               ("fitness:history", "History", "albright_fitness:history"),
               ("fitness:programs", "Programs", "albright_fitness:programs"),
               ("fitness:exercises", "Exercises", "albright_fitness:exercises")),
        stylesheet="albright_fitness/fitness.css", script="albright_fitness/fitness.js"),
    App(key="admin", label="Admin", short_label="Admin", brand="Albright Admin", accent="admin",
        color="#94A3B8", landing_url_name="albright_control:overview",
        namespaces=("albright_control", "albright_resume_admin", "labs_admin"),
        description="System health, integrations, AI spend, ideas and the audit log.",
        last_app_cookie="admin", tile="admin", superuser_only=True, stylesheet="albright_control/inbox.css",
        pages=(("admin:overview", "Overview", "albright_control:overview"),
               ("admin:ideas", "Ideas", "albright_control:ideas"),
               ("admin:health", "System health", "albright_control:health"))),
)

BY_KEY = {app.key: app for app in APPS}
BY_NAMESPACE = {ns: app for app in APPS for ns in app.namespaces}
BY_COOKIE = {app.last_app_cookie: app for app in APPS}
# "Where I land" page key -> (app, label, URL name)
PAGES = {key: (app, label, url_name) for app in APPS for key, label, url_name in app.pages}

# Home (the Labs hub) - not an app, but it has a brand and accent of its own.
HOME_NAMESPACE, HOME_ACCENT, HOME_BRAND = "labs", "labs", "Albright Labs"
HOME_LABEL = "Labs"                     # the header's name beside the logo on Home and the landing page
# Pages with no app of their own (the public resume, Django's /admin/, the Bot API pages):
# the header shows them as the trading app, as it always has.
DEFAULT_APP = BY_KEY["trade"]


def grantable_keys():
    """Apps a superuser can give a user (Admin isn't one - it comes with being a superuser)."""
    return [app.key for app in APPS if not app.superuser_only]


def all_keys():
    return [app.key for app in APPS]


def app_for_namespace(namespace):
    """The app a URL namespace belongs to, or None (Home, the public resume, the Bot API...)."""
    return BY_NAMESPACE.get(namespace)
