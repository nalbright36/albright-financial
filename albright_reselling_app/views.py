from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.urls import reverse
from . import calculator_views, ledger_metrics
from .forms import ScanRequestForm, HarvestRequestForm, AnalysisRequestForm
from .models import ScanRequest, HarvestRequest, HistoricalLot, AnalysisRequest
from .scanner.dashboard import get_scanner_dashboard_context
from django.db.models import Avg, Count


@login_required
def dashboard(request):
    user = request.user

    # Old (pre-scanner-integration) research-pipeline stats used to live
    # here - removed from the dashboard only; ScannedLot/HistoricalLot/
    # AnalysisRequest etc. and their own pages are untouched, see
    # auction_scanner.html/historical_data.html/sleeper_segments.html.
    context = {"ledger_tiles": ledger_metrics.dashboard_tiles(user)}
    context.update(get_scanner_dashboard_context(user=user))
    # The slide-in Calculator panel (_calculator_panel.html) - same shared
    # context the full Calculators page uses, see calculator_views.panel_context().
    context.update(calculator_views.panel_context(user))
    return render(request, "dashboard.html", context)


@login_required
def ledger(request):
    """Legacy URL (every old bookmark/link to plain /ledger/, and the
    dashboard's own ?status=unsold deep links) - redirects to the
    Reselling tab, preserving the query string, instead of 404ing or
    silently dropping ?status=/?edit=. See ledger_views.ledger_resale for
    the page this used to render directly."""
    url = reverse("albright_reselling_app:ledger_resale")
    query = request.META.get("QUERY_STRING", "")
    if query:
        url = f"{url}?{query}"
    return redirect(url)


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