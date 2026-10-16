"""Views for the on-demand AI resale review feature (scanner/ai_review.py +
scanner/ai_review_service.py). Kept out of views.py per project convention -
this file is only ever imported from urls.py.

Two endpoints, deliberately separate URLs rather than one shared
"<int:pk>/" for both: POST takes a lot id and may create a new AIReview;
GET takes an existing review's id. Sharing one URL/id between those two
different object types is exactly the kind of mix-up worth avoiding here,
since e.g. a "re-run" button on the review page needs the lot's id, not the
review's.
"""
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import BidWatchForm
from .scanner.ai_review_service import ProviderUnavailable, ReviewLimitExceeded, review_lot
from .scanner.bid_watch import default_max_bid
from .scanner.dashboard import _has_maxsold, _time_remaining, _watched_lots
from .scanner.review_history import (
    CATEGORY_CHOICES,
    CONFIDENCE_CHOICES,
    OUTCOME_CHOICES,
    PAGE_SIZE,
    SOURCE_CHOICES,
    filtered_reviews,
    filters_from_params,
    reviews_to_csv,
    row_for,
    summary_stats,
    time_remaining_at_review,
)
from .scanner_models import AIReview, BidWatch, LotUserEstimate, SourcedLot

RECENT_REVIEW_WINDOW = timedelta(hours=24)


@login_required
def request_review(request, lot_id):
    """POST only (in practice - the dashboard only ever links here via a
    form). Reuses a recent successful review instead of paying again,
    unless force=1 was sent."""
    lot = get_object_or_404(SourcedLot, pk=lot_id)
    force = request.POST.get("force") == "1"

    if not force:
        cutoff = timezone.now() - RECENT_REVIEW_WINDOW
        recent = (
            AIReview.objects.filter(lot=lot, status="done", created_at__gte=cutoff)
            .order_by("-created_at").first()
        )
        if recent:
            return redirect("albright_reselling_app:ai_review_detail", review_id=recent.pk)

    try:
        review = review_lot(lot)
    except (ReviewLimitExceeded, ProviderUnavailable) as exc:
        messages.error(request, str(exc))
        return redirect("albright_reselling_app:dashboard")

    return redirect("albright_reselling_app:ai_review_detail", review_id=review.pk)


@login_required
def review_detail(request, review_id):
    review = get_object_or_404(
        AIReview.objects.select_related("lot", "lot__evaluation"), pk=review_id
    )
    lot = review.lot
    evaluation = getattr(lot, "evaluation", None)
    result = review.result or {}
    time_remaining = _time_remaining(lot, timezone.now()) if lot.end_time else "unknown"
    lot_is_open = lot.end_time is None or lot.end_time > timezone.now()

    return render(request, "ai_review_detail.html", {
        "review": review,
        "lot": lot,
        "evaluation": evaluation,
        "lot_is_open": lot_is_open,
        "time_remaining": time_remaining,
        "time_remaining_at_review": time_remaining_at_review(review),
        "identified_items": result.get("identified_items") or [],
        "comps": result.get("comps") or [],
        "dropped_comps": result.get("dropped_comps") or 0,
        "notes": result.get("notes") or [],
        "red_flags": result.get("red_flags") or [],
        "summary": result.get("summary") or "",
    })


@login_required
def watch_lot(request, lot_id):
    """Set (or edit) a watchlisted lot's max bid - pre-filled with the AI
    review's suggested max bid if one exists, else the scanner's own max
    bid (see scanner.bid_watch.default_max_bid), unless the watch already
    has a max bid of its own, which always wins. The quick one-click
    "Add to Watchlist" star (toggle_watch below) is what actually creates
    the BidWatch now - landing here with no watch yet at all (e.g. an old
    bookmark) just creates one on save, same as before."""
    lot = get_object_or_404(SourcedLot.objects.select_related("evaluation"), pk=lot_id)
    evaluation = getattr(lot, "evaluation", None)
    existing = getattr(lot, "bid_watch", None)

    review_id = request.GET.get("review") or request.POST.get("review")
    review = None
    if review_id:
        review = AIReview.objects.filter(pk=review_id, lot=lot, status="done").first()
    if review is None:
        review = AIReview.objects.filter(lot=lot, status="done").order_by("-created_at").first()

    if request.method == "POST":
        form = BidWatchForm(request.POST, instance=existing)
        if form.is_valid():
            watch = form.save(commit=False)
            watch.lot = lot
            watch.save()
            messages.success(request, f'Watching "{lot.title}" - max ${watch.my_max_bid}.')
            return redirect("albright_reselling_app:dashboard")
    elif existing is not None and existing.my_max_bid is not None:
        form = BidWatchForm(instance=existing)
    else:
        default = default_max_bid(evaluation, review)
        form = BidWatchForm(instance=existing, initial={"my_max_bid": default} if default is not None else None)

    return render(request, "watch_lot.html", {
        "lot": lot, "evaluation": evaluation, "review": review, "form": form, "existing": existing,
    })


@login_required
@require_POST
def toggle_watch(request, lot_id):
    """The "Add to Watchlist" star - one click adds a lot to the
    watchlist with no max bid yet (set later via watch_lot above, or
    never - it's optional); clicking again removes it outright. Only for
    a lot whose auction is still open - matches the dashboard's own rule
    that every Act Now/Watch table row is already open-only, but checked
    again here since this is reachable directly by URL too."""
    lot = get_object_or_404(SourcedLot, pk=lot_id)
    existing = getattr(lot, "bid_watch", None)

    if existing is not None:
        existing.delete()
        return JsonResponse({"watching": False})

    if lot.end_time is not None and lot.end_time <= timezone.now():
        return JsonResponse({"error": "This lot has already closed."}, status=400)

    BidWatch.objects.create(lot=lot, status="watching")
    return JsonResponse({"watching": True})


@login_required
@require_POST
def use_my_max_bid(request, lot_id):
    """One click "Use as my max bid": copies the lot's saved estimate's
    my_max_bid (LotUserEstimate, from the Calculator dialog) onto its
    Watchlist entry - only when that entry has no max bid of its own yet,
    so it never silently overwrites one set by hand (edit that via
    watch_lot instead). JSON for the dialog/Watchlist's fetch() calls;
    a plain form POST (no JS) redirects back to the dashboard."""
    lot = get_object_or_404(SourcedLot, pk=lot_id)
    watch = BidWatch.objects.filter(lot=lot).first()
    estimate = LotUserEstimate.objects.filter(lot=lot).first()
    wants_json = request.headers.get("x-requested-with") == "XMLHttpRequest"

    if watch is None or estimate is None or watch.my_max_bid is not None:
        if watch is None:
            error = "This lot isn't on your watchlist."
        elif estimate is None:
            error = "No saved estimate for this lot."
        else:
            error = "This lot already has a max bid - edit it instead."
        if wants_json:
            return JsonResponse({"error": error}, status=400)
        messages.error(request, error)
        return redirect("albright_reselling_app:dashboard")

    watch.my_max_bid = estimate.my_max_bid
    watch.save(update_fields=["my_max_bid"])
    if wants_json:
        return JsonResponse({"watch_max_bid": float(watch.my_max_bid)})
    messages.success(request, f'Max bid for "{lot.title}" set to ${watch.my_max_bid}.')
    return redirect("albright_reselling_app:dashboard")


@login_required
def watchlist_section(request):
    """Just the Watchlist card's body (table or empty state) - fetched by
    static/js/auction_scanner.js after a lot is added via any table's star,
    so the newly-watched lot shows up there immediately without a full
    page reload, without duplicating this markup in JS. See
    _watchlist_section.html and dashboard.html's #watchlist-body."""
    now = timezone.now()
    watched_lots = _watched_lots(now)
    return render(request, "_watchlist_section.html", {
        "watched_lots": watched_lots, "watched_lots_has_maxsold": _has_maxsold(watched_lots),
    })


@login_required
def review_history(request):
    """Every past AI review, newest first, filterable/paginated via GET
    params so a filtered view stays a bookmarkable/shareable link. Sorting
    within the current page is handled client-side by the existing
    static/js/auction_scanner.js (table.holdings + th[data-sort-key])."""
    filters = filters_from_params(request.GET)
    qs = filtered_reviews(filters)

    if request.GET.get("export") == "csv":
        response = HttpResponse(reviews_to_csv(qs), content_type="text/csv")
        response["Content-Disposition"] = 'attachment; filename="ai_reviews.csv"'
        return response

    query_params = request.GET.copy()
    query_params.pop("page", None)
    query_params.pop("export", None)
    filter_querystring = query_params.urlencode()

    page = Paginator(qs, PAGE_SIZE).get_page(request.GET.get("page"))
    rows = [row_for(review) for review in page.object_list]

    return render(request, "ai_review_history.html", {
        "rows": rows,
        "page": page,
        "filters": filters,
        "filter_querystring": filter_querystring,
        "summary": summary_stats(),
        "source_choices": SOURCE_CHOICES,
        "category_choices": CATEGORY_CHOICES,
        "confidence_choices": CONFIDENCE_CHOICES,
        "status_choices": AIReview.STATUS_CHOICES,
        "outcome_choices": OUTCOME_CHOICES,
    })
