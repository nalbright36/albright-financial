from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from .forms import LedgerEntryForm, LedgerEntryFormSet, ScanRequestForm, HarvestRequestForm, AnalysisRequestForm
from .models import LedgerEntry, ScanRequest, HarvestRequest, HistoricalLot, ScannedLot, AnalysisRequest, ReconciliationRequest, DeepDiveRequest
from django.db.models import Avg, Count, Sum, F
from django.utils import timezone


@login_required
def dashboard(request):
    user = request.user

    # --- Ledger / business health ---
    ledger_entries = LedgerEntry.objects.filter(owner=user)
    in_inventory = list(ledger_entries.filter(sold_for__isnull=True))
    sold_entries = list(ledger_entries.filter(sold_for__isnull=False))

    in_inventory_count = len(in_inventory)
    in_inventory_cost = sum(e.total for e in in_inventory)
    sold_count = len(sold_entries)
    sold_profit = sum(e.profit for e in sold_entries)

    now = timezone.now()
    this_month = ledger_entries.filter(created_at__year=now.year, created_at__month=now.month)
    month_totals = this_month.aggregate(fees=Sum("fees"), shipping=Sum("shipping"), cost=Sum("cost"))

    # --- Scanner ---
    scanned_lots = ScannedLot.objects.filter(scan_request__owner=user)
    total_scanned = scanned_lots.count()
    flagged_60 = scanned_lots.filter(interest_score__gte=60).count()
    top_flagged = scanned_lots.order_by("-interest_score")[:3]
    recent_scans = ScanRequest.objects.filter(owner=user)[:3]

    # --- Historical data ---
    historical_lots = HistoricalLot.objects.filter(owner=user)
    total_harvested = historical_lots.count()
    total_harvests = HarvestRequest.objects.filter(owner=user).count()

    # --- Sleeper analysis ---
    analyzed_lots = list(historical_lots.filter(estimated_resale_low__isnull=False, final_price__isnull=False))
    total_analyzed = len(analyzed_lots)
    margins = [l.margin_pct for l in analyzed_lots if l.margin_pct is not None]
    avg_margin_pct = sum(margins) / len(margins) if margins else None

    top_segment = None
    auctioneer_agg = {}
    for lot in analyzed_lots:
        if not lot.auctioneer_name:
            continue
        entry = auctioneer_agg.setdefault(lot.auctioneer_name, {"count": 0, "hits": 0})
        entry["count"] += 1
        if lot.margin and lot.margin > 0:
            entry["hits"] += 1
    candidates = [(name, v["hits"] / v["count"], v["count"]) for name, v in auctioneer_agg.items() if v["count"] >= 3]
    if candidates:
        candidates.sort(key=lambda c: -c[1])
        top_segment = {"name": candidates[0][0], "hit_rate": candidates[0][1] * 100, "count": candidates[0][2]}

    # --- Reconciliation accuracy ---
    reconciled_lots = scanned_lots.filter(actual_price_realized__isnull=False)
    reconciled_count = reconciled_lots.count()
    under_max_hammer = reconciled_lots.filter(
        max_hammer__isnull=False, actual_price_realized__lte=F("max_hammer")
    ).count() if reconciled_count else 0

    # --- Pending background jobs, across all five queues ---
    pending_jobs = []
    for qs, label in [
        (ScanRequest.objects.filter(owner=user, status__in=["pending", "running"]), "Scan"),
        (HarvestRequest.objects.filter(owner=user, status__in=["pending", "running"]), "Harvest"),
        (ReconciliationRequest.objects.filter(owner=user, status__in=["pending", "running"]), "Reconciliation"),
        (DeepDiveRequest.objects.filter(owner=user, status__in=["pending", "running"]), "Deep Dive"),
        (AnalysisRequest.objects.filter(owner=user, status__in=["pending", "running"]), "Analysis"),
    ]:
        for obj in qs:
            pending_jobs.append({"label": label, "status": obj.get_status_display(), "created_at": obj.created_at})
    pending_jobs.sort(key=lambda j: j["created_at"], reverse=True)

    return render(request, "dashboard.html", {
        "in_inventory_count": in_inventory_count,
        "in_inventory_cost": in_inventory_cost,
        "sold_count": sold_count,
        "sold_profit": sold_profit,
        "month_fees": month_totals["fees"] or 0,
        "month_shipping": month_totals["shipping"] or 0,
        "month_cost": month_totals["cost"] or 0,
        "total_scanned": total_scanned,
        "flagged_60": flagged_60,
        "top_flagged": top_flagged,
        "recent_scans": recent_scans,
        "total_harvested": total_harvested,
        "total_harvests": total_harvests,
        "total_analyzed": total_analyzed,
        "avg_margin_pct": avg_margin_pct,
        "top_segment": top_segment,
        "reconciled_count": reconciled_count,
        "under_max_hammer": under_max_hammer,
        "pending_jobs": pending_jobs[:5],
    })


@login_required
def ledger(request):
    queryset = LedgerEntry.objects.filter(owner=request.user)
    add_form = LedgerEntryForm(prefix="add")
    formset = LedgerEntryFormSet(queryset=queryset)

    if request.method == "POST":
        if "add_entry" in request.POST:
            add_form = LedgerEntryForm(request.POST, prefix="add")
            if add_form.is_valid():
                entry = add_form.save(commit=False)
                entry.owner = request.user
                entry.save()
                return redirect("albright_reselling_app:ledger")
        elif "save_ledger" in request.POST:
            formset = LedgerEntryFormSet(request.POST, queryset=queryset)
            if formset.is_valid():
                instances = formset.save(commit=False)
                for instance in instances:
                    instance.owner = request.user
                    instance.save()
                for obj in formset.deleted_objects:
                    obj.delete()
                return redirect("albright_reselling_app:ledger")

    entries = list(queryset)
    totals = {
        "cost": sum((e.cost or 0) for e in entries),
        "fees": sum((e.fees or 0) for e in entries),
        "shipping": sum((e.shipping or 0) for e in entries),
        "total": sum((e.total or 0) for e in entries),
        "sold_for": sum((e.sold_for or 0) for e in entries),
        "profit": sum((e.profit or 0) for e in entries),
    }

    return render(request, "ledger.html", {
        "add_form": add_form,
        "formset": formset,
        "totals": totals,
    })


@login_required
def auction_scanner(request):
    if request.method == "POST":
        form = ScanRequestForm(request.POST, user=request.user)
        if form.is_valid():
            scan = form.save(commit=False)
            scan.owner = request.user
            scan.save()
            return redirect("albright_reselling_app:auction_scanner")
    else:
        form = ScanRequestForm(user=request.user)

    scans = ScanRequest.objects.filter(owner=request.user)
    return render(request, "auction_scanner.html", {"form": form, "scans": scans})


@login_required
def scan_detail(request, scan_id):
    scan = ScanRequest.objects.filter(owner=request.user).get(pk=scan_id)
    lots = scan.lots.order_by("-discount_likelihood_score") if scan.reference_analysis_id else scan.lots.all()
    return render(request, "scan_detail.html", {"scan": scan, "lots": lots})

@login_required
def historical_data(request):
    if request.method == "POST":
        form = HarvestRequestForm(request.POST)
        if form.is_valid():
            harvest = form.save(commit=False)
            harvest.owner = request.user
            harvest.save()
            return redirect("albright_reselling_app:historical_data")
    else:
        form = HarvestRequestForm()

    harvests = HarvestRequest.objects.filter(owner=request.user)
    total_lots = HistoricalLot.objects.filter(owner=request.user).count()
    return render(request, "historical_data.html", {
        "form": form,
        "harvests": harvests,
        "total_lots": total_lots,
    })

MIN_SAMPLE_SIZE = 3  # minimum lots before a segment counts as a real pattern, not noise

@login_required
def sleeper_segments(request):
    if request.method == "POST":
        form = AnalysisRequestForm(request.POST)
        if form.is_valid():
            analysis = form.save(commit=False)
            analysis.owner = request.user
            analysis.save()
            return redirect("albright_reselling_app:sleeper_segments")
    else:
        form = AnalysisRequestForm()

    historical = HistoricalLot.objects.filter(owner=request.user, final_price__isnull=False)

    by_category = list(
        historical.exclude(category="")
        .values("category")
        .annotate(lot_count=Count("id"), avg_price=Avg("final_price"), avg_bid_count=Avg("final_bid_count"))
        .filter(lot_count__gte=MIN_SAMPLE_SIZE)
        .order_by("avg_bid_count")
    )
    by_auctioneer = list(
        historical.exclude(auctioneer_name="")
        .values("auctioneer_name")
        .annotate(lot_count=Count("id"), avg_price=Avg("final_price"), avg_bid_count=Avg("final_bid_count"))
        .filter(lot_count__gte=MIN_SAMPLE_SIZE)
        .order_by("avg_bid_count")
    )

    analyses = AnalysisRequest.objects.filter(owner=request.user)

    return render(request, "sleeper_segments.html", {
        "by_category": by_category,
        "by_auctioneer": by_auctioneer,
        "total_historical": historical.count(),
        "min_sample_size": MIN_SAMPLE_SIZE,
        "form": form,
        "analyses": analyses,
    })


@login_required
def analysis_results(request, analysis_id):
    analysis = AnalysisRequest.objects.filter(owner=request.user).get(pk=analysis_id)
    lots = list(analysis.analyzed_lots.filter(estimated_resale_low__isnull=False))
    lots.sort(key=lambda l: (l.margin if l.margin is not None else -999999), reverse=True)
    return render(request, "analysis_results.html", {"analysis": analysis, "lots": lots})