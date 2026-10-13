"""Views for the Today page (/today/) and the Selling Board
(/ledger/board/) - logic lives in selling.py; this file is just request
plumbing, the same split as ledger_views.py/scanner_views.py.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import calculator_views, selling
from .forms import EditPriceForm, MarkListedForm
from .models import LedgerEntry


def _safe_next(request, default_name):
    """Only ever redirects within this app - a GET/POST "next" is just a
    convenience to return to whichever page (Board or Today) sent the
    user here, never an open redirect."""
    next_url = request.POST.get("next") or request.GET.get("next")
    if next_url and next_url.startswith("/"):
        return redirect(next_url)
    return redirect(default_name)


@login_required
def today_page(request):
    sections = selling.today_sections(request.user)
    # The slide-in Calculator panel (_calculator_panel.html) - same shared
    # context the full Calculators page uses, see calculator_views.panel_context().
    sections.update(calculator_views.panel_context(request.user))
    return render(request, "today.html", sections)


@login_required
def board_page(request):
    ledger_type = request.GET.get("type")
    if ledger_type not in ("resale", "personal"):
        ledger_type = None
    columns = selling.board_columns(request.user, ledger_type=ledger_type)
    return render(request, "board.html", {
        "columns": columns, "ledger_type": ledger_type or "", "ledger_tab": "board",
    })


@login_required
def mark_listed(request, entry_id):
    """Asks for list price, platform, and an optional URL (task's "Mark
    listed" button) - sets listed_at to today the first time this is
    submitted (an "Edit price"-only re-submission, from the board, routes
    through edit_price instead, so this never clobbers the original
    listed_at)."""
    entry = get_object_or_404(LedgerEntry, pk=entry_id, owner=request.user)
    if request.method == "POST":
        form = MarkListedForm(request.POST, instance=entry)
        if form.is_valid():
            entry = form.save(commit=False)
            if entry.listed_at is None:
                entry.listed_at = timezone.localdate()
            entry.save()
            messages.success(request, f"Marked {entry.item} as listed.")
            return _safe_next(request, "albright_reselling_app:ledger_board")
    else:
        form = MarkListedForm(instance=entry)
    return render(request, "mark_listed.html", {
        "entry": entry, "form": form, "next": request.GET.get("next", ""),
    })


@login_required
@require_POST
def mark_shipped(request, entry_id):
    """Marks every not-yet-shipped sale on this entry as shipped right
    now - the Board/Today card represents the whole item, not one sale."""
    entry = get_object_or_404(LedgerEntry.objects.prefetch_related("sales"), pk=entry_id, owner=request.user)
    now = timezone.now()
    updated = entry.sales.filter(shipped_at__isnull=True).update(shipped_at=now)
    if updated:
        messages.success(request, f"Marked {entry.item} shipped.")
    return _safe_next(request, "albright_reselling_app:ledger_board")


@login_required
@require_POST
def markdown_done(request, entry_id):
    """The markdown step's "Done" button - records last_markdown_step so
    it stops showing as due."""
    entry = get_object_or_404(LedgerEntry, pk=entry_id, owner=request.user)
    status = selling.markdown_status(entry)
    if status["step"]:
        entry.last_markdown_step = status["step"]
        entry.save(update_fields=["last_markdown_step"])
        messages.success(request, f"Marked the {status['step']}-day step done for {entry.item}.")
    return _safe_next(request, "albright_reselling_app:ledger_board")


@login_required
def edit_price(request, entry_id):
    """The card's plain "Edit price" button, and (with ?step=N) a
    markdown step's "Lower price" action - same form either way; the
    step variant also records last_markdown_step."""
    entry = get_object_or_404(LedgerEntry, pk=entry_id, owner=request.user)
    step = request.POST.get("step") or request.GET.get("step") or ""

    if request.method == "POST":
        form = EditPriceForm(request.POST)
        if form.is_valid():
            entry.list_price = form.cleaned_data["list_price"]
            update_fields = ["list_price"]
            if step:
                entry.last_markdown_step = int(step)
                update_fields.append("last_markdown_step")
            entry.save(update_fields=update_fields)
            messages.success(request, f"Updated {entry.item}'s price.")
            return _safe_next(request, "albright_reselling_app:ledger_board")
    else:
        form = EditPriceForm(initial={"list_price": entry.list_price})

    return render(request, "edit_price.html", {
        "entry": entry, "form": form, "step": step, "next": request.GET.get("next", ""),
    })
