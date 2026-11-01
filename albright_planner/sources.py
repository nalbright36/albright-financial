"""What other apps put on the Planner, and the Meals overlay.

Sources (all OFF by default - nothing is added unless the user turns one on in
Planner settings, and can open the source's app):
  watchlist      Reselling watchlist - each watched lot's auction end time, a
                 view-only event linking to the lot.
  opportunities  Opportunities - "Apply by" on saved ones with a deadline, and
                 "Follow up" a week after applying with no movement (tasks).
  markdown       Reselling ledger - markdown steps that are due (tasks).
sync() brings a user's source items up to date through services.upsert_source_item.
It runs only when they save Planner settings - never on page loads, signals,
scans or schedules. A source that's off, or whose app they can't open, has its
items archived. Items the user deleted are never made again.

Meals are NOT copied: meals() reads the plan live for the dates on screen."""
import logging
from dataclasses import dataclass, field
from datetime import timedelta

from django.urls import reverse
from django.utils import timezone

from . import services
from .models import PlannerSettings

log = logging.getLogger(__name__)
FOLLOW_UP_AFTER = timedelta(days=7)
OPEN_PIPELINE = ("saved", "applied", "talking", "working")
UNSOLD = ("holding", "partially_sold")


def can_open(user, key):
    from albright_labs import app_access
    from albright_labs.models import UserPreference
    return app_access.can_use(user, UserPreference.for_user(user), key)


def _local(moment):
    moment = timezone.localtime(moment)
    return moment.date(), moment.time().replace(second=0, microsecond=0)


# ---------------------------------------------------------------------------
# Sources: each returns [{"id", "title", "date", "time", "checkable", "url"}] for one user
# ---------------------------------------------------------------------------

def _watchlist(user, today):
    from albright_reselling_app.models import BidWatch
    rows = []
    for watch in BidWatch.objects.active().filter(lot__end_time__isnull=False).select_related("lot"):
        day, at = _local(watch.lot.end_time)
        rows.append({"id": watch.lot_id, "title": f"Auction ends: {watch.lot.title}", "date": day, "time": at,
                     "checkable": False, "url": watch.lot.url})
    return rows


def _opportunities(user, today):
    from albright_crypto_app.models import Opportunity
    rows = []
    qs = Opportunity.objects.filter(status__in=OPEN_PIPELINE, duplicate_of__isnull=True)
    for opp in qs.filter(status="saved").exclude(application_deadline__isnull=True, deadline__isnull=True):
        day, at = _local(opp.any_deadline)
        rows.append({"id": f"deadline:{opp.pk}", "title": f"Apply by: {opp.title}", "date": day, "time": at,
                     "checkable": True, "url": reverse("albright_crypto_app:opportunity", args=[opp.pk])})
    for opp in qs.filter(status="applied", applied_at__isnull=False):
        since = opp.stage_since
        if opp.followup_sent_at and opp.followup_sent_at >= since:
            continue                                   # already followed up since it was applied
        rows.append({"id": f"followup:{opp.pk}", "title": f"Follow up: {opp.title}",
                     "date": timezone.localtime(since + FOLLOW_UP_AFTER).date(), "time": None, "checkable": True,
                     "url": reverse("albright_crypto_app:opportunity", args=[opp.pk])})
    return rows


def _markdown(user, today):
    from albright_reselling_app.models import LedgerEntry
    from albright_reselling_app.selling import markdown_status
    rows = []
    for entry in LedgerEntry.objects.filter(owner=user, status__in=UNSOLD, listed_at__isnull=False):
        status = markdown_status(entry, today=today)
        if status["due"] and status["step"]:
            rows.append({"id": f"{entry.pk}:{status['step']}", "title": f"{status['label']}: {entry.item}",
                         "date": status["due_date"], "time": None, "checkable": True,
                         "url": reverse("albright_reselling_app:ledger_manage", args=[entry.pk])})
    return rows


@dataclass(frozen=True)
class Source:
    key: str          # ScheduleItem.source_app
    label: str
    help: str
    app: str          # the app it needs access to (apps_registry key)
    rows: callable = field(repr=False)


SOURCES = (
    Source("watchlist", "Automatically add auction end times from my Watchlist",
           "View-only events that link to the lot.", "resell", _watchlist),
    Source("opportunities", "Automatically add application deadlines and follow-ups from Opportunities",
           "Tasks: “Apply by” for saved opportunities with a deadline, and “Follow up” a week "
           "after applying.", "opps", _opportunities),
    Source("markdown", "Automatically add due markdown steps from my Reselling ledger",
           "Tasks to check off when a listed item's next markdown is due.", "resell", _markdown),
)
SOURCE_KEYS = [s.key for s in SOURCES]


def available(user):
    """The sources this user could turn on (they can open the source's app)."""
    return [s for s in SOURCES if can_open(user, s.app)]


def sync(user, today=None):
    """Bring the user's source items up to date (only the sources they turned on). Returns {source key: number of
    records found} for the sources run. Called from Planner settings only."""
    prefs = PlannerSettings.for_user(user)
    now = timezone.now()
    today = today or timezone.localdate()
    done = {}
    for source in SOURCES:
        if not (prefs.source_on(source.key) and can_open(user, source.app)):
            services.archive_source_items(user, source.key)
            continue
        try:
            rows = source.rows(user, today)
            for row in rows:
                services.upsert_source_item(user, source.key, row["id"], row["title"], row["date"], row["time"],
                                            checkable=row["checkable"], url=row["url"])
            services.remove_source_items(user, source.key, keep=[r["id"] for r in rows])
            done[source.key] = len(rows)
        except Exception:                              # one broken source never takes the Planner down
            log.exception("Planner source %s failed for user %s", source.key, user.pk)
    PlannerSettings.objects.filter(pk=prefs.pk).update(synced_at=now)
    return done


# ---------------------------------------------------------------------------
# The Meals overlay (read live, never copied)
# ---------------------------------------------------------------------------

SLOT_ORDER = {"breakfast": 0, "lunch": 1, "dinner": 2, "snack": 3}


@dataclass
class Meal:
    """A planned meal shown in the Planner - view only (no checkbox, no drag)."""
    entry: object
    date: object
    title: str
    slot: str
    url: str
    is_meal: bool = True


def show_meals(user):
    return can_open(user, "meals") and PlannerSettings.for_user(user).show_meals


def meals(user, start, end):
    """Planned (and cooked) meals between start and end for someone who can open Meals - else none."""
    if not show_meals(user):
        return []
    from albright_meals.models import MealPlanEntry
    entries = MealPlanEntry.objects.filter(date__gte=start, date__lte=end).exclude(status="skipped") \
        .select_related("recipe")
    out = [Meal(e, e.date, e.display_title, e.get_slot_display(), reverse("albright_meals:plan_entry", args=[e.pk]))
           for e in entries]
    out.sort(key=lambda m: (m.date, SLOT_ORDER.get(m.entry.slot, 9)))
    return out
