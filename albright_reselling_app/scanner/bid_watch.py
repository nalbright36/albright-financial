"""BidWatch: the watchlist and its archive. Pieces:
  - default_max_bid(): the "set max bid" form's pre-fill value.
  - soft_remove() / hard_delete() / restore(): the Watchlist page's
    "Remove" (archive it, keep it for the stats), "Delete" (gone, never
    tracked or analyzed), and re-adding an archived lot.
  - archive_closed_watches(): a lot still watched when its auction closes
    is archived automatically ("closed_on_watchlist").
  - resolve_watches(): once an archived lot's auction has ended, finds its
    final price (already-tracked, or one search_closed() lookup) and sets
    its outcome vs. compare_max (below/at/above, or unknown after
    RESOLVE_GIVE_UP_AFTER) - plus, for a lot that closed on the watchlist,
    the older likely_won/lost/unknown status and an optional result alert
    (ALERTS["watchlist_result_alerts"], off by default).

The "closing soon" Telegram push (the lot's own alert, as opposed to a
likely-won/lost result) is NOT here - that's the watchlist_alerts
management command's job, reading straight from the database on its own
schedule, so there's exactly one place in the codebase that sends it
(see that command's docstring for why scan_lots used to send it too, and
no longer does).

resolve_watches() is global (every source, every call) and is called
unconditionally from every scan_lots run, the same way scanner.alerts.
run_alerts() already is - a lot closing on MaxSold still needs to be
checked even if this particular scan_lots invocation was --source
shopgoodwill. Telegram sending itself is scanner.alerts.send_telegram();
this module owns the BidWatch lifecycle, not the wire format of alerts.

A result alert (when enabled) ignores quiet hours by construction:
ALERTS["quiet_hours"] (scanner/alerts.py) only gates run_alerts()'s
per-lot kinds (candidate/lead/zero_bid/relisted) - this sweep calls
send_telegram() directly and was never routed through that gate.
"""
import logging
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.urls import reverse
from django.utils import timezone

from ..scanner_models import AlertSent, BidWatch
from .adapters.base import SourceBlocked, SourceUnavailable
from .adapters.hibid import HiBidAdapter
from .adapters.maxsold import MaxSoldAdapter
from .adapters.shopgoodwill import ShopGoodwillAdapter
from .alerts import SITE_BASE_URL, send_telegram
from .dashboard import _source_display, watch_my_bid

log = logging.getLogger(__name__)

ADAPTERS = {"shopgoodwill": ShopGoodwillAdapter, "maxsold": MaxSoldAdapter, "hibid": HiBidAdapter}
RESOLVE_GIVE_UP_AFTER = timedelta(days=3)  # no final price found this long after the end -> outcome "unknown"
AT_TOLERANCE = Decimal("0.01")              # final price within this of compare_max counts as "at"


def default_max_bid(evaluation, review):
    """Pre-fill for the "set max bid" form: the AI review's suggested
    max bid if one exists, else the scanner's own max bid."""
    if review is not None and review.suggested_max_bid is not None:
        return review.suggested_max_bid
    if evaluation is not None:
        return evaluation.max_bid
    return None


def _search_closed_for_lot(adapter, lot, now):
    """search_closed()'s signature differs by adapter - ShopGoodwill and
    HiBid both take a lookback window (MaxSold's API has no equivalent
    parameter), and it needs to cover however long ago this specific lot
    actually ended, not just the default couple of days."""
    if lot.source in ("shopgoodwill", "hibid"):
        days_back = max(2, (now - lot.end_time).days + 1)
        return adapter.search_closed(lot.title, days_back=days_back)
    return adapter.search_closed(lot.title)


def _find_final_price(lot, adapter, now):
    """One search_closed() lookup (using the lot's own title as the
    query), scanning for a result matching this lot's external_id. Stops
    as soon as a match is found rather than exhausting every page."""
    try:
        for raw in _search_closed_for_lot(adapter, lot, now):
            if raw.external_id == lot.external_id:
                return Decimal(str(raw.current_price)).quantize(Decimal("0.01"))
        return None
    except (SourceBlocked, SourceUnavailable) as exc:
        log.warning("Watch resolution lookup failed for lot %s: %s", lot.pk, exc)
        return None
    finally:
        adapter.pause()


def _send_result_alert(watch):
    """Off by default - ALERTS["watchlist_result_alerts"]. Likely_won/lost
    is still settled either way (_resolve_one always runs); this only
    gates the Telegram push about it."""
    cfg = settings.RESELLING_SCANNER.get("ALERTS", {})
    if not cfg.get("watchlist_result_alerts", False):
        return False
    if watch.result_alert_sent:
        return False
    lot = watch.lot
    lines = [f"<b>Bid result: {watch.get_status_display()}</b>", f"{_source_display(lot.source)} · {lot.title[:120]}"]
    if lot.final_price is not None:
        max_bid_text = f"${watch.my_max_bid:.2f}" if watch.my_max_bid is not None else "not set"
        lines.append(f"Final price: ${lot.final_price:.2f} · your max was {max_bid_text}")
    if watch.status == "likely_won":
        win_url = SITE_BASE_URL + reverse("albright_reselling_app:ledger_win_lot", args=[lot.pk])
        lines.append(f'<a href="{win_url}">Log it in the ledger</a>')
    lines.append(f'<a href="{lot.url}">View listing</a>')
    send_telegram("\n".join(lines))
    watch.result_alert_sent = True
    watch.save(update_fields=["result_alert_sent"])
    return True


# ---------------------------------------------------------------------------
# Leaving the watchlist: soft remove (archive), hard delete, re-add
# ---------------------------------------------------------------------------

def archive_watch(watch, reason, now=None):
    """Moves a watch to the archive, snapshotting what its final price will
    be compared against: the same "My bid" the Watchlist page shows -
    my max bid if set ("manual"), else the scanner's max bid ("scanner"),
    else nothing (outcome will be "unknown")."""
    compare_max, source = watch_my_bid(watch)
    watch.archived_at = now or timezone.now()
    watch.archive_reason = reason
    watch.compare_max = compare_max
    watch.compare_source = {"mine": "manual", "scanner": "scanner"}.get(source, "")
    watch.save(update_fields=["archived_at", "archive_reason", "compare_max", "compare_source"])
    return watch


def soft_remove(watch, now=None):
    """The Watchlist page's "Remove": archived (archive_reason
    "soft_removed"), not deleted - it still gets a final price and an
    outcome, and counts in the Archive's stats."""
    return archive_watch(watch, "soft_removed", now)


def hard_delete(watch):
    """The Watchlist page's "Delete": the BidWatch is gone, along with the
    lot's watchlist AlertSent rows - nothing about it is tracked,
    analyzed, or protected from prune_lots any more."""
    AlertSent.objects.filter(lot_id=watch.lot_id, kind="watchlist").delete()
    watch.delete()


def restore(watch):
    """Back on the watchlist from the archive (re-adding a soft-removed
    lot that's still open) - clears every archive/outcome field."""
    watch.status = "watching"
    watch.archived_at = None
    watch.archive_reason = ""
    watch.compare_max = None
    watch.compare_source = ""
    watch.final_price = None
    watch.outcome = None
    watch.resolved_at = None
    watch.save()
    return watch


def archive_closed_watches(now=None):
    """Every still-active watch whose auction has ended is archived as
    "closed_on_watchlist" (compare_max snapshotted now). No network - safe
    to call from the Watchlist page itself, as well as every sweep."""
    now = now or timezone.now()
    watches = BidWatch.objects.active().filter(lot__end_time__lte=now).select_related("lot", "lot__evaluation")
    return [archive_watch(watch, "closed_on_watchlist", now) for watch in watches]


# ---------------------------------------------------------------------------
# Resolving archived watches' outcomes
# ---------------------------------------------------------------------------

def outcome_for(final_price, compare_max):
    """below/at/above vs. compare_max ("at" within AT_TOLERANCE either way),
    or "unknown" when there was nothing to compare against."""
    if compare_max is None:
        return "unknown"
    if abs(final_price - compare_max) <= AT_TOLERANCE:
        return "at"
    return "below" if final_price < compare_max else "above"


def _final_price(lot, adapters_cache, now, lookup):
    """The lot's final price: already tracked (track_closed, or an earlier
    lookup), else - when lookup is allowed - one search_closed() title
    lookup, saved back onto the lot."""
    if lot.is_closed and lot.final_price is not None:
        return lot.final_price
    if not lookup:
        return None
    adapter = adapters_cache.get(lot.source)
    if adapter is None:
        adapter = ADAPTERS[lot.source]()
        adapters_cache[lot.source] = adapter
    final_price = _find_final_price(lot, adapter, now)
    if final_price is not None:
        lot.final_price = final_price
        lot.is_closed = True
        lot.final_checked_at = now
        lot.save(update_fields=["final_price", "is_closed", "final_checked_at"])
    return final_price


def _settle_status(watch, final_price):
    """The older watching -> likely_won/lost/unknown status (Today page,
    dashboard tile, digest, result alert), only for a lot that was still
    on the watchlist when it closed - a soft-removed lot was never bid on."""
    if watch.archive_reason != "closed_on_watchlist":
        return False
    if final_price is None or watch.my_max_bid is None:
        watch.status = "unknown"  # no final price, or no max bid on record to compare against
    else:
        watch.status = "likely_won" if final_price <= watch.my_max_bid else "lost"
    return True


def _resolve_one(watch, adapters_cache, now, lookup):
    """Returns the watch if its outcome was settled this call, else None
    (still waiting - try again on the next sweep)."""
    lot = watch.lot
    final_price = _final_price(lot, adapters_cache, now, lookup)

    if final_price is not None:
        watch.final_price = final_price
        watch.outcome = outcome_for(final_price, watch.compare_max)
    elif now - lot.end_time > RESOLVE_GIVE_UP_AFTER:
        watch.outcome = "unknown"
    else:
        return None

    settled_status = _settle_status(watch, final_price)
    watch.resolved_at = now
    watch.save(update_fields=["final_price", "outcome", "status", "resolved_at"])
    if settled_status:
        _send_result_alert(watch)
    return watch


def resolve_watches(now=None, lookup=True):
    """The sweep, run from every scan_lots pass (and, lookup=False, from
    track_closed right after it records final prices): archives any
    watch whose lot just closed, then settles every ended, archived,
    still-unresolved watch it can - one search_closed() lookup (plus its
    pause) per lot still missing a final price, adapters reused across
    lots of the same source. lookup=False never touches the network, it
    only uses final prices already on the lot."""
    now = now or timezone.now()
    archive_closed_watches(now)
    watches = (
        BidWatch.objects.archived().filter(outcome__isnull=True, lot__end_time__lte=now).select_related("lot")
    )
    adapters_cache = {}
    resolved = []
    for watch in watches:
        result = _resolve_one(watch, adapters_cache, now, lookup)
        if result is not None:
            resolved.append(result)
    return resolved
