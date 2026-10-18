"""Backfill the Watchlist Archive for watches that already ended before it
existed - otherwise they'd be neither on the watchlist (closed) nor in
the archive. Each one is archived as "closed_on_watchlist" at its lot's
end time, with compare_max = its own max bid (else the scanner's max bid)
and, when the lot's final price is already known, final_price/outcome
("at" = within $0.01). Unresolved ones are left for the next resolution
sweep (scanner/bid_watch.resolve_watches). Logic is inlined (migrations
can't rely on app code staying the same).
"""
from decimal import Decimal

from django.db import migrations

AT_TOLERANCE = Decimal("0.01")


def backfill(apps, schema_editor):
    BidWatch = apps.get_model("albright_reselling_app", "BidWatch")
    LotEvaluation = apps.get_model("albright_reselling_app", "LotEvaluation")
    from django.utils import timezone

    now = timezone.now()
    watches = BidWatch.objects.filter(archived_at__isnull=True, lot__end_time__lte=now).select_related("lot")
    for watch in watches:
        lot = watch.lot
        if watch.my_max_bid is not None:
            compare_max, compare_source = watch.my_max_bid, "manual"
        else:
            evaluation = LotEvaluation.objects.filter(lot_id=lot.pk).first()
            if evaluation is not None and evaluation.max_bid and evaluation.max_bid > 0:
                compare_max, compare_source = evaluation.max_bid, "scanner"
            else:
                compare_max, compare_source = None, ""
        watch.archived_at = lot.end_time
        watch.archive_reason = "closed_on_watchlist"
        watch.compare_max = compare_max
        watch.compare_source = compare_source
        if lot.is_closed and lot.final_price is not None:
            watch.final_price = lot.final_price
            if compare_max is None:
                watch.outcome = "unknown"
            elif abs(lot.final_price - compare_max) <= AT_TOLERANCE:
                watch.outcome = "at"
            else:
                watch.outcome = "below" if lot.final_price < compare_max else "above"
            watch.resolved_at = watch.resolved_at or now
        elif watch.status == "unknown":
            watch.outcome = "unknown"  # the old sweep already gave up looking
        watch.save()


class Migration(migrations.Migration):

    dependencies = [
        ("albright_reselling_app", "0031_bidwatch_archive"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
