"""BidWatch: the watchlist. Two pieces:
  - default_max_bid(): the "set max bid" form's pre-fill value.
  - resolve_watches(): once a watched lot's end_time has passed, finds its
    final price (already-tracked, or one search_closed() lookup) and
    settles the watch to likely_won/lost/unknown, with an optional result
    alert (ALERTS["watchlist_result_alerts"], off by default).

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

from ..scanner_models import BidWatch
from .adapters.base import SourceBlocked, SourceUnavailable
from .adapters.hibid import HiBidAdapter
from .adapters.maxsold import MaxSoldAdapter
from .adapters.shopgoodwill import ShopGoodwillAdapter
from .alerts import SITE_BASE_URL, send_telegram
from .dashboard import _source_display

log = logging.getLogger(__name__)

ADAPTERS = {"shopgoodwill": ShopGoodwillAdapter, "maxsold": MaxSoldAdapter, "hibid": HiBidAdapter}
RESOLVE_GIVE_UP_AFTER = timedelta(days=2)


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


def _resolve_one(watch, adapters_cache, now):
    """Returns the watch if it was settled this call, else None (still
    waiting - try again on the next sweep)."""
    lot = watch.lot
    final_price = lot.final_price if lot.is_closed else None

    if final_price is None:
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

    if final_price is not None:
        if watch.my_max_bid is None:
            watch.status = "unknown"  # no max bid on record - nothing to compare the final price against
        else:
            watch.status = "likely_won" if final_price <= watch.my_max_bid else "lost"
    elif now - lot.end_time > RESOLVE_GIVE_UP_AFTER:
        watch.status = "unknown"
    else:
        return None

    watch.resolved_at = now
    watch.save(update_fields=["status", "resolved_at"])
    _send_result_alert(watch)
    return watch


def resolve_watches(now=None):
    """Settles every past-due "watching" BidWatch it can, one
    search_closed() lookup (plus its pause) per lot still needing one.
    Adapters are reused across lots of the same source within one call."""
    now = now or timezone.now()
    watches = (
        BidWatch.objects.filter(status="watching", lot__end_time__lte=now).select_related("lot")
    )
    adapters_cache = {}
    resolved = []
    for watch in watches:
        result = _resolve_one(watch, adapters_cache, now)
        if result is not None:
            resolved.append(result)
    return resolved
