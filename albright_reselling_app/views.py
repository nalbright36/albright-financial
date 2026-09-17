from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from .forms import LedgerEntryForm, LedgerEntryFormSet, ScanRequestForm, HarvestRequestForm, AnalysisRequestForm
from .models import LedgerEntry, ScanRequest, HarvestRequest, HistoricalLot, AnalysisRequest
from django.db.models import Avg, Count


@login_required
def dashboard(request):
    return render(request, "dashboard.html")


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
        form = ScanRequestForm(request.POST)
        if form.is_valid():
            scan = form.save(commit=False)
            scan.owner = request.user
            scan.save()
            return redirect("albright_reselling_app:auction_scanner")
    else:
        form = ScanRequestForm()

    scans = ScanRequest.objects.filter(owner=request.user)
    return render(request, "auction_scanner.html", {
        "form": form,
        "scans": scans,
    })


@login_required
def scan_detail(request, scan_id):
    scan = ScanRequest.objects.filter(owner=request.user).get(pk=scan_id)
    lots = scan.lots.all()
    return render(request, "scan_detail.html", {
        "scan": scan,
        "lots": lots,
    })

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