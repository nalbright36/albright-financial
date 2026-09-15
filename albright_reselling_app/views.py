from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from .forms import LedgerEntryForm, LedgerEntryFormSet, ScanRequestForm
from .models import LedgerEntry, ScanRequest


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