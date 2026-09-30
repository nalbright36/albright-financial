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
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .scanner.ai_review_service import ProviderUnavailable, ReviewLimitExceeded, review_lot
from .scanner.dashboard import _time_remaining
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
from .scanner_models import AIReview, SourcedLot

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

    return render(request, "ai_review_detail.html", {
        "review": review,
        "lot": lot,
        "evaluation": evaluation,
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
