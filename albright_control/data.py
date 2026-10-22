"""The Admin area's Data page: row counts, the prune_lots dry run, and the
backup downloads (CSV files in a zip)."""
import csv
import io
import json
import zipfile
from datetime import date, datetime
from decimal import Decimal

from django.apps import apps
from django.core.management import call_command

COUNTED_MODELS = [
    ("Reseller", ["albright_reselling_app.SourcedLot", "albright_reselling_app.LotEvaluation",
                  "albright_reselling_app.AIReview", "albright_reselling_app.BidWatch",
                  "albright_reselling_app.AlertSent", "albright_reselling_app.ScanRun", "albright_reselling_app.SpotPrice",
                  "albright_reselling_app.LedgerEntry", "albright_reselling_app.LedgerSale",
                  "albright_reselling_app.LedgerExpense", "albright_reselling_app.LotUserEstimate"]),
    ("Opportunities", ["albright_crypto_app.Opportunity", "albright_crypto_app.Source", "albright_crypto_app.Earning",
                       "albright_crypto_app.TimeLog", "albright_crypto_app.AIExtraction", "albright_crypto_app.Walkthrough",
                       "albright_crypto_app.ApplicationDraft", "albright_crypto_app.ProcessedItem"]),
    ("Trading", ["albright_trading_app.Strategy", "albright_trading_app.Trade", "albright_trading_app.OptionStrategy",
                 "albright_trading_app.OptionTrade"]),
    ("Bot API & Admin", ["albright_bot_api.BotToken", "albright_bot_api.BotRequestLog", "albright_bot_api.SiteSuggestion",
                         "albright_control.JobRun", "albright_control.AuditEntry"]),
]


def row_counts():
    groups = []
    for group, labels in COUNTED_MODELS:
        rows = []
        for label in labels:
            try:
                model = apps.get_model(label)
            except LookupError:
                continue
            rows.append({"model": model._meta.verbose_name_plural.capitalize(), "label": label,
                         "count": model.objects.count()})
        groups.append({"group": group, "rows": rows})
    return groups


def prune_dry_run():
    out = io.StringIO()
    call_command("prune_lots", "--dry-run", stdout=out, stderr=out)
    return out.getvalue()


# ---------------------------------------------------------------------------
# Backups
# ---------------------------------------------------------------------------

def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return f"{value:f}"
    if isinstance(value, (dict, list)):
        return json.dumps(value, default=str)
    return str(value)


def _csv(header, rows):
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    for row in rows:
        writer.writerow([_cell(v) for v in row])
    return out.getvalue()


def _model_csv(queryset, exclude=(), extra=None):
    """All concrete fields of a model (foreign keys as their id), plus extra (name, fn) columns."""
    fields = [f for f in queryset.model._meta.concrete_fields if f.name not in exclude]
    extra = extra or []
    header = [f.attname for f in fields] + [name for name, _fn in extra]
    return _csv(header, ([getattr(obj, f.attname) for f in fields] + [fn(obj) for _n, fn in extra]
                         for obj in queryset.iterator()))


def _ledger():
    from albright_reselling_app.models import LedgerEntry, LedgerSale
    return {"ledger_entries.csv": _model_csv(LedgerEntry.objects.order_by("pk"), extra=[("owner", lambda e: e.owner.username)]),
            "ledger_sales.csv": _model_csv(LedgerSale.objects.order_by("pk"))}


def _expenses():
    from albright_reselling_app.models import LedgerExpense
    return {"expenses.csv": _model_csv(LedgerExpense.objects.order_by("pk"), extra=[("owner", lambda e: e.owner.username)])}


def _closed_lots():
    """Closed scanner lots (final price known), with their evaluation and every feature as its own column."""
    from albright_reselling_app.scanner_models import SourcedLot
    lots = list(SourcedLot.objects.exclude(final_price=None).select_related("evaluation").order_by("pk"))
    feature_keys = sorted({k for lot in lots for k in (lot.features or {})})
    base = ["id", "source", "external_id", "title", "url", "end_time", "current_price", "final_price", "final_checked_at"]
    evaluation = ["category", "max_bid", "headroom", "confidence", "is_candidate", "is_lead"]

    def row(lot):
        ev = getattr(lot, "evaluation", None)
        return ([getattr(lot, f, None) for f in base] + [getattr(ev, f, None) if ev else None for f in evaluation]
                + [(lot.features or {}).get(k) for k in feature_keys])
    return {"closed_lots.csv": _csv(base + [f"eval_{f}" for f in evaluation] + [f"feature_{k}" for k in feature_keys],
                                    (row(lot) for lot in lots))}


def _watchlist_archive():
    from albright_reselling_app.scanner_models import BidWatch
    qs = BidWatch.objects.exclude(archived_at=None).select_related("lot").order_by("pk")
    return {"watchlist_archive.csv": _model_csv(qs, extra=[("lot_title", lambda w: w.lot.title),
                                                           ("lot_url", lambda w: w.lot.url),
                                                           ("lot_source", lambda w: w.lot.source)])}


def _opportunities():
    from albright_crypto_app.models import Earning, Opportunity
    return {"opportunities.csv": _model_csv(Opportunity.objects.order_by("pk"), exclude=("raw_text",)),
            "earnings.csv": _model_csv(Earning.objects.order_by("pk"))}


BACKUPS = {
    "ledger": ("Ledger entries and sales", _ledger),
    "expenses": ("Expenses", _expenses),
    "closed-lots": ("Closed scanner lots with features", _closed_lots),
    "watchlist-archive": ("Watchlist archive", _watchlist_archive),
    "opportunities": ("Opportunities and earnings", _opportunities),
}


def backup_zip(key):
    """(filename, zip bytes, {csv name: rows}) for one backup."""
    _label, build = BACKUPS[key]
    files = build()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    counts = {name: max(text.count("\n") - 1, 0) for name, text in files.items()}
    return f"albright-{key}-{date.today().isoformat()}.zip", buffer.getvalue(), counts
