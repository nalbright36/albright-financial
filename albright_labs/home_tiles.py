"""Home's tiles (hub.html "My tiles"): the catalog of every page and quick action a user can pin, and each user's tiles.

Two kinds of tile:
  page    opens that page's main view (never one item) - Tile(..., kind=PAGE), the default.
  action  a "create / log" action, marked with a small "+" in the icon's corner. With action="<name>" it opens that
          quick-add popup right on Home (albright_labs/quick_actions.py: the popup's fields, posting to the app's own
          save view); with no action it just goes to its URL (Start workout); action="ask_ai" opens the assistant.

BUILDING A NEW FEATURE: every new page a person would open from Home, and every new "create / log" action, gets a
Tile below in the same change - a page as kind=PAGE, an action with _act(...) (plus its popup in quick_actions.py
when it can be done in a few fields). Keys must be unique, start with their group's key, and are never renamed
(they're stored in HomeTile.key). Every app also gets a "whole app" tile (key = the app's key, going to its landing
page) without being listed. A badge is the name of a function below that takes the user and returns a count (one
cheap query); counts are cached per user for 60 seconds (badges()). needs: a function below (user -> bool) for a tile
only some people may use.

Only tiles for apps the user can open and that aren't archived are shown or offered (visible_catalog). A user's
HomeTile rows for an app they lose are kept, just not shown - they come back with the app.

Groups that aren't apps: "Account" (everyone signed in: My account, Circles, API keys...), "AI assistant" (people
allowed the assistant: Ask AI, its settings, Visuals) and "Visuals" (each saved Visual the user owns or has been shared
is a tile, "visual.<id>", built per user - catalog(user)).
"""
import logging
from dataclasses import dataclass

from django.core.cache import cache
from django.db import transaction
from django.urls import NoReverseMatch, reverse

from . import app_access, apps_registry
from .archive import is_app_archived

log = logging.getLogger(__name__)

BADGE_TTL = 60
APP = "app"            # icon: the app's own launcher icon (templates/_app_icon.html)
PAGE, ACTION = "page", "action"


@dataclass(frozen=True)
class Tile:
    key: str              # stored in HomeTile.key - never rename one
    label: str            # under the icon (one line) and in the Add sheet
    icon: str             # a symbol in albright_labs/_tile_icons.html, or APP
    app: str              # apps_registry key (or a group below): the color, and who may see it
    url_name: str
    description: str      # the Add sheet's second line
    badge: str = ""       # a function below: user -> int
    suffix: str = ""      # appended to the URL - a query or #anchor
    args: tuple = ()      # the URL's arguments (a Visual's id)
    kind: str = PAGE      # PAGE or ACTION
    action: str = ""      # an ACTION's popup (quick_actions.ACTIONS) - empty: the tile just opens its URL
    needs: str = ""       # a function below: user -> bool, for a tile only some people may use

    @property
    def app_info(self):
        return apps_registry.BY_KEY.get(self.app) or GROUPS.get(self.app) or VISUALS_GROUP

    @property
    def color(self):
        return self.app_info.color

    @property
    def accent(self):
        return self.app_info.accent

    @property
    def is_action(self):
        return self.kind == ACTION

    @property
    def url(self):
        try:
            return reverse(self.url_name, args=self.args) + self.suffix
        except NoReverseMatch:       # an archived app's URLs can be missing in some setups
            return ""


def _act(key, label, icon, app, url_name, description, action="", **kw):
    """An action tile: url_name is where it goes without JavaScript (and its popup's "More details")."""
    return Tile(key, label, icon, app, url_name, description, kind=ACTION, action=action, **kw)


PAGES = (
    # Meals
    Tile("meals.groceries", "Grocery list", "cart", "meals", "albright_meals:groceries", "Shows items left",
         badge="grocery_left"),
    Tile("meals.recipes", "Recipes", "book", "meals", "albright_meals:recipes", "Your recipe book"),
    Tile("meals.week", "This week's meals", "fork", "meals", "albright_meals:week", "Meal plan"),
    Tile("meals.ideas", "Meal ideas", "spark", "meals", "albright_meals:ideas", "What to cook next"),
    Tile("meals.eat_out", "Eat Out", "pin", "meals", "albright_meals:eat_out", "Find a restaurant"),
    _act("meals.recipe_add", "Add recipe", "book", "meals", "albright_meals:recipe_add", "Paste a link or type it in",
         action="recipe"),
    _act("meals.grocery_add", "Add groceries", "cart", "meals", "albright_meals:groceries",
         "One per line, or a comma list", action="groceries"),
    _act("meals.plan_add", "Plan a meal", "fork", "meals", "albright_meals:week", "Pick a recipe, day and meal",
         action="plan_meal"),
    # Fitness
    Tile("fitness.today", "Today", "dumb", "fitness", "albright_fitness:dashboard", "Today's workout"),
    Tile("fitness.food", "Food diary", "book", "fitness", "albright_fitness:food", "What you ate today"),
    Tile("fitness.programs", "Programs", "list", "fitness", "albright_fitness:programs", "Your training programs"),
    Tile("fitness.exercises", "Exercises", "dumb", "fitness", "albright_fitness:exercises", "The exercise library"),
    Tile("fitness.challenges", "Challenges", "trophy", "fitness", "albright_fitness:challenges", "Compete with friends"),
    Tile("fitness.history", "History", "clock", "fitness", "albright_fitness:history", "Past workouts"),
    Tile("fitness.insights", "Insights", "chart", "fitness", "albright_fitness:insights", "Trends and records"),
    Tile("fitness.body", "Body", "scale", "fitness", "albright_fitness:body", "Weight and measurements"),
    Tile("fitness.targets", "Targets", "chart", "fitness", "albright_fitness:targets", "Calories and macros"),
    Tile("fitness.my_foods", "My foods", "food", "fitness", "albright_fitness:my_foods", "Foods you've saved"),
    Tile("fitness.food_ai", "Log food with AI", "spark", "fitness", "albright_fitness:food_ai",
         "Connect your own AI to the diary"),
    _act("fitness.log_food", "Log food", "food", "fitness", "albright_fitness:food_find", "Find a food and log it",
         action="food"),
    _act("fitness.log_weight", "Log weight", "scale", "fitness", "albright_fitness:body", "Weigh in",
         action="weight", suffix="#fx-weigh"),
    _act("fitness.quick_log", "Quick log workout", "dumb", "fitness", "albright_fitness:dashboard",
         "“bench 185x8,8,7, run 3.1mi”", action="workout"),
    _act("fitness.start", "Start workout", "dumb", "fitness", "albright_fitness:start", "Opens the workout screen"),
    # Planner
    Tile("planner.today", "Today", "cal", "planner", "albright_planner:day", "Open tasks today", badge="tasks_today"),
    Tile("planner.week", "Week", "cal", "planner", "albright_planner:week", "This week at a glance"),
    Tile("planner.month", "Month", "cal", "planner", "albright_planner:month", "The month view"),
    Tile("planner.briefing", "Briefing", "list", "planner", "roundup:briefing", "Your week ahead, in short"),
    Tile("planner.settings", "Planner settings", "filter", "planner", "albright_planner:settings",
         "Time zone, weather, workouts"),
    _act("planner.new", "Add task", "cal", "planner", "albright_planner:new", "Title, day, time, repeat",
         action="task"),
    _act("planner.event", "Add event", "cal", "planner", "albright_planner:new", "Something with a time",
         action="event", suffix="?kind=event"),
    # Notes
    Tile("notes.list", "All notes", "note", "notes", "albright_notes:list", "Every note"),
    _act("notes.new", "New note", "note", "notes", "albright_notes:list", "A title and a few lines",
         action="note", suffix="?start=new"),
    _act("notes.checklist", "Quick checklist", "list", "notes", "albright_notes:list", "One item per line",
         action="checklist"),
    # Money
    Tile("money.transactions", "Transactions", "list", "money", "albright_money:transactions", "Everything spent"),
    Tile("money.budget", "Budget", "chart", "money", "albright_money:budget", "This month's budget"),
    Tile("money.plans", "Budget plans", "chart", "money", "albright_money:budget_plans", "Plans from notes"),
    Tile("money.accounts", "Accounts", "card", "money", "albright_money:accounts", "Balances"),
    Tile("money.transfer", "Transfer", "card", "money", "albright_money:transfer", "Move money between accounts"),
    Tile("money.import", "Import", "upload", "money", "albright_money:import", "Bank CSV import"),
    Tile("money.categories", "Categories", "tag", "money", "albright_money:categories", "Spending categories"),
    Tile("money.rules", "Rules", "filter", "money", "albright_money:rules", "Auto-categorize"),
    _act("money.add", "Add transaction", "card", "money", "albright_money:transactions",
         "“42.18 Publix groceries”", action="transaction"),
    # Opportunities
    Tile("opps.new", "New opps", "star", "opps", "albright_crypto_app:inbox", "New since your last visit",
         badge="opps_new"),
    Tile("opps.pipeline", "Pipeline", "list", "opps", "albright_crypto_app:pipeline", "What you're pursuing"),
    Tile("opps.earnings", "Earnings", "card", "opps", "albright_crypto_app:earnings", "What you've earned"),
    Tile("opps.hidden", "Hidden", "filter", "opps", "albright_crypto_app:hidden", "Ones you've hidden"),
    Tile("opps.profile", "My profile", "star", "opps", "albright_crypto_app:profile", "What fits you"),
    Tile("opps.settings", "Opps settings", "filter", "opps", "albright_crypto_app:settings", "Sources and filters"),
    _act("opps.add", "Add opportunity", "star", "opps", "albright_crypto_app:add", "Paste a link and its text",
         action="opportunity"),
    # Admin
    Tile("admin.health", "System health", "chart", "admin", "albright_control:health", "Late or failing jobs"),
    Tile("admin.ideas", "Ideas", "spark", "admin", "albright_control:ideas", "Suggestions inbox"),
    Tile("admin.suggestions", "Suggestions", "list", "admin", "albright_control:suggestions", "The review list"),
    Tile("admin.users", "Users", "list", "admin", "albright_control:users", "Accounts and app access"),
    Tile("admin.integrations", "Integrations", "filter", "admin", "albright_control:integrations", "Keys and services"),
    Tile("admin.ai_usage", "AI usage", "chart", "admin", "albright_control:ai_usage", "Spend and limits"),
    Tile("admin.bot_api", "Bot API", "spark", "admin", "albright_control:bot_api", "Keys and activity"),
    Tile("admin.data", "Data", "list", "admin", "albright_control:data", "Tables and counts"),
    Tile("admin.storage", "Storage", "upload", "admin", "albright_control:storage", "Files and space used"),
    Tile("admin.audit", "Audit log", "clock", "admin", "albright_control:audit", "Who changed what"),
    _act("admin.idea_add", "Submit an idea", "spark", "admin", "albright_control:idea_add", "A title and a line or two",
         action="idea"),
    # Account (everyone signed in)
    Tile("account.settings", "My account", "filter", "account", "labs:account", "Apps, sign-in page, storage"),
    Tile("account.circles", "Circles", "star", "account", "labs:circles", "Who you share apps with"),
    Tile("account.people", "People", "list", "account", "labs:people", "People you share with"),
    Tile("account.shared", "Shared with me", "list", "account", "labs:shared_with_me", "What others share"),
    Tile("account.sharing", "Sharing defaults", "filter", "account", "labs:sharing_defaults", "Who sees new items"),
    Tile("account.activity", "Activity", "clock", "account", "labs:activity", "Recent changes"),
    Tile("account.notifications", "Notifications", "clock", "account", "labs:notification_settings",
         "What you're told about"),
    Tile("account.api_keys", "API keys", "filter", "account", "labs:api_keys", "Connect your own AI",
         needs="api_keys_allowed"),
    # AI assistant (people allowed it)
    _act("assistant.ask", "Ask AI", "spark", "assistant", "assistant:setup", "Opens the assistant", action="ask_ai"),
    Tile("assistant.settings", "AI settings", "filter", "assistant", "assistant:setup", "Your assistant's model"),
    Tile("assistant.visuals", "Visuals", "chart", "assistant", "assistant:visuals", "Charts the AI made for you"),
)

# Badges on whole-app tiles (the design shows the Planner's open tasks on its app tile).
APP_BADGES = {"planner": "tasks_today"}


def _app_tile(app):
    return Tile(app.key, app.label, APP, app.key, app.landing_url_name, f"{app.label} (whole app) · app home",
                badge=APP_BADGES.get(app.key, ""))


class _Group:
    """A group that isn't an app: like an App to the templates (key, label, accent, color)."""
    def __init__(self, key, label, accent, color):
        self.key, self.label, self.accent, self.color = key, label, accent, color


VISUALS = "visuals"
VISUALS_GROUP = _Group(VISUALS, "Visuals", "labs", "#2DD4BF")
ACCOUNT, ASSISTANT = "account", "assistant"
GROUPS = {ACCOUNT: _Group(ACCOUNT, "Account", "labs", "#94A3B8"),
          ASSISTANT: _Group(ASSISTANT, "AI assistant", "labs", "#2DD4BF"),
          VISUALS: VISUALS_GROUP}


def visual_tiles(user):
    """A tile per saved Visual this user owns or has been shared (newest first, at most 40)."""
    try:
        from albright_assistant.models import Visual

        from . import sharing
    except ImportError:                                 # pragma: no cover
        return []
    qs = sharing.visible_queryset(user, Visual, Visual.objects.filter(saved=True)).order_by("-updated_at")[:40]
    return [Tile(f"visual.{v.pk}", v.title[:60], "chart", VISUALS, "assistant:visual",
                 "Your chart" if v.owner_id == user.pk else "Shared with you", args=(v.pk,)) for v in qs]


def catalog(user=None):
    """{key: Tile} - every app's whole-app tile, then PAGES (built from the registry on each call, so a new app
    gets its tile without being listed here), then - for a user - their Visuals."""
    tiles = [t for t in [_app_tile(app) for app in apps_registry.APPS] + list(PAGES)
             if t.app in apps_registry.BY_KEY or t.app in GROUPS]
    if user is not None:
        tiles = [t for t in tiles if not t.needs or _needs(t.needs, user)]
    if user is not None and getattr(user, "is_authenticated", False):
        from .request_cache import get
        tiles += get(("visual-tiles", user.pk), lambda: visual_tiles(user))     # Home asks twice: once per request
    return {t.key: t for t in tiles}


def _pref(user):
    from .models import UserPreference
    return UserPreference.for_user(user)


def _openable(user, pref=None):
    """The app keys this user can open, archived ones left out (allowed_keys already does - checked again so a
    superuser, who may open archived apps, never gets their tiles)."""
    pref = pref or _pref(user)
    groups = [ACCOUNT] + ([ASSISTANT] if assistant_allowed(user) else []) + [VISUALS]
    return [k for k in app_access.allowed_keys(user, pref) if not is_app_archived(k)] + groups


def visible_catalog(user, pref=None):
    """Every tile this user may add, grouped by app in their app order: [(App, [Tile, ...]), ...] - the app's
    whole-app tile first."""
    groups, all_tiles = [], catalog(user).values()
    for key in _openable(user, pref):
        tiles = [t for t in all_tiles if t.app == key and t.url]
        if tiles:
            groups.append((apps_registry.BY_KEY.get(key) or GROUPS.get(key) or VISUALS_GROUP, tiles))
    return groups


def seed(user, pref=None):
    """A user with no tiles gets one per app they see on Home now (their order, Admin last), so nothing disappears."""
    from .models import HomeTile
    pref = pref or _pref(user)
    keys = [k for k in app_access.nav_keys(user, pref) if not is_app_archived(k)]
    keys = [k for k in keys if k != "admin"] + [k for k in keys if k == "admin"]
    with transaction.atomic():
        if HomeTile.objects.filter(user=user).exists():
            return
        HomeTile.objects.bulk_create([HomeTile(user=user, key=k, order=i) for i, k in enumerate(keys)],
                                     ignore_conflicts=True)


def user_tiles(user, pref=None):
    """This user's tiles, in order - only ones in the catalog whose app they can open now (others stay saved)."""
    from .models import HomeTile
    if not HomeTile.objects.filter(user=user).exists():
        seed(user, pref)
    openable = set(_openable(user, pref))
    keys = HomeTile.objects.filter(user=user).order_by("order", "pk").values_list("key", flat=True)
    tiles = catalog(user)
    return [tiles[k] for k in keys if k in tiles and tiles[k].app in openable and tiles[k].url]


def can_add(user, key, pref=None):
    tile = catalog(user).get(key)
    return bool(tile and tile.app in _openable(user, pref) and tile.url)


def add(user, key):
    from django.db.models import Max

    from .models import HomeTile
    last = HomeTile.objects.filter(user=user).aggregate(m=Max("order"))["m"]
    HomeTile.objects.get_or_create(user=user, key=key, defaults={"order": 0 if last is None else last + 1})


def remove(user, key):
    from .models import HomeTile
    HomeTile.objects.filter(user=user, key=key).delete()


def reorder(user, keys):
    """Save the order shown on Home. Tiles not in `keys` (hidden ones, for apps the user can't open now) keep
    their place after the shown ones."""
    from .models import HomeTile
    rows = {t.key: t for t in HomeTile.objects.filter(user=user)}
    seen = [k for k in dict.fromkeys(keys) if k in rows]
    rest = sorted((t for k, t in rows.items() if k not in seen), key=lambda t: (t.order, t.pk))
    changed = []
    for i, tile in enumerate([rows[k] for k in seen] + rest):
        if tile.order != i:
            tile.order = i
            changed.append(tile)
    HomeTile.objects.bulk_update(changed, ["order"])


# Who may use a tile (Tile.needs) and a group ------------------------------------------------------------------

def _needs(name, user):
    try:
        return bool(globals()[name](user))
    except Exception:  # noqa: BLE001 - a check must never break Home
        log.exception("Home tile check %s failed", name)
        return False


def api_keys_allowed(user):
    from albright_bot_api.scopes import keys_allowed
    return keys_allowed(user)


def assistant_allowed(user):
    """The AI assistant group: only for people allowed the assistant (albright_assistant.access)."""
    try:
        from albright_assistant import access
    except ImportError:                                 # pragma: no cover
        return False
    from .request_cache import get
    return get(("assistant-allowed", getattr(user, "pk", None)), lambda: access.allowed(user))


# Badges -------------------------------------------------------------------------------------------------------

def grocery_left(user):
    """Unchecked items on the grocery list (the Meals space they last used)."""
    from albright_meals.models import GroceryItem

    from .circles import current_space
    space = current_space(user, "meals")
    return 0 if space is None else GroceryItem.objects.active().filter(space=space, checked=False).count()


def tasks_today(user):
    """Today's open tasks, overdue ones included (the Planner's own count)."""
    from albright_planner.planner import open_tasks_today
    todays, overdue = open_tasks_today(user)
    return len(todays) + len(overdue)


def opps_new(user):
    """Inbox items found since the user last opened the Inbox (UserPreference.opps_seen_at)."""
    from albright_crypto_app.inbox_views import inbox_queryset
    seen = _pref(user).opps_seen_at
    qs = inbox_queryset(user)
    return (qs.filter(discovered_at__gt=seen) if seen else qs).count()


def _cache_key(user):
    return f"home-tile-badges:{user.pk}"


def badges(user, tiles):
    """{tile key: count} for these tiles' badges (0 included) - cached per user for BADGE_TTL seconds. A failing
    badge is left out (no badge), never an error."""
    wanted = {t.key: t.badge for t in tiles if t.badge}
    cached = cache.get(_cache_key(user)) or {}
    by_fn = {}
    for key, fn in wanted.items():
        if fn not in cached:
            if fn not in by_fn:
                try:
                    by_fn[fn] = int(globals()[fn](user))
                except Exception:  # noqa: BLE001 - a badge must never break Home
                    log.exception("Home tile badge %s failed", fn)
                    by_fn[fn] = None
    if by_fn:
        cached = {**cached, **{fn: n for fn, n in by_fn.items() if n is not None}}
        cache.set(_cache_key(user), cached, BADGE_TTL)
    return {key: cached[fn] for key, fn in wanted.items() if fn in cached}


def forget_badges(user):
    cache.delete(_cache_key(user))
