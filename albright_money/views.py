"""Albright Money pages (/money/). Opening any of them needs Money access (Admin > Users -
albright_labs.app_access 404s everyone else first). Money lives in spaces: your Personal space and circles that share
Money (albright_labs.circles). Each page shows one space - the switcher (?space=<id>, remembered) - and the dashboard
also has "All my spaces". Every object is looked up through visible_to, so anything in a space you don't see is a
404."""
from datetime import date
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction as db_transaction
from django.db.models import Count, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_POST

from . import budget as budgets
from . import imports, money, quickadd
from .forms import AccountForm, CategoryForm, MappingForm, RuleForm, SplitRows, TransactionForm, TransferForm, UploadForm
from .models import ZERO, Account, Category, Rule, Split, Transaction

LIST_LIMIT = 300


def _wants_json(request):
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _next(request, fallback):
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target and url_has_allowed_host_and_scheme(target, {request.get_host()}, require_https=request.is_secure()):
        return target
    return reverse(fallback)


def _month(value, default):
    try:
        year, month = (int(p) for p in (value or "").split("-")[:2])
        return date(year, month, 1)
    except (TypeError, ValueError):
        return default


def _scope(request):
    """(owner, space): the space this page shows - ?space= / space= when the user sees it, else the one they last
    picked, else Personal. Also request.space / request.spaces, for the switcher."""
    request.spaces = money.visible_spaces(request.user)
    request.space = money.current_space(request.user, request.POST.get("space") or request.GET.get("space"))
    if request.space is None:
        raise Http404
    return request.user, request.space


def _in(space, url):
    return f"{url}{'&' if '?' in url else '?'}space={space.pk}"


def _accounts(user):
    return Account.objects.visible_to(user)


def _rows(accounts):
    values = money.balances(accounts)
    out = []
    for a in accounts:
        label, shown = money.shown_balance(a, values[a.pk])
        out.append({"account": a, "label": label, "shown": shown, "balance": values[a.pk]})
    return out


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@login_required
@never_cache
def dashboard(request):
    user, month = request.user, budgets.this_month()
    _owner, space = _scope(request)
    everything = request.GET.get("all") == "1"
    views = [row for row in budgets.budgets_for(user, month) if everything or row[1].pk == space.pk]
    accounts = list(_accounts(user).filter(archived=False).select_related("space__circle", "space__user", "owner"))
    if not everything:
        accounts = [a for a in accounts if a.space_id == space.pk]
    visible = Transaction.objects.visible_to(user)
    categories = list(Category.objects.visible_to(user).filter(kind=Category.EXPENSE))
    if not everything:
        visible = visible.filter(account__space=space)
        categories = [c for c in categories if c.space_id == space.pk]
    net = money.category_totals(categories, month, budgets.month_end(month))
    top = sorted(({"category": c, "spent": money.spent(c, net[c.pk])} for c in categories if c.pk in net),
                 key=lambda r: r["spent"], reverse=True)
    top = [r for r in top if r["spent"] > 0][:5]
    spent = sum((v["spent"] for _l, _h, v in views), ZERO)
    available = sum((v["available"] for _l, _h, v in views), ZERO)
    return render(request, "albright_money/dashboard.html", {
        "month": month, "budgets": views, "safe_to_spend": budgets.left_this_month(user),
        "spent": spent, "available": available, "spent_status": budgets.status(spent, available),
        "spent_pct": budgets.bar(spent, available),
        "accounts": _rows(accounts),
        "review": visible.filter(needs_review=True).select_related("account", "category")[:8],
        "review_count": visible.filter(needs_review=True).count(),
        "top": top, "top_max": max([r["spent"] for r in top] or [Decimal(1)]),
        "latest": visible.select_related("account", "category").prefetch_related("splits")[:10],
        "space": space, "all_spaces": everything, "all_url": reverse("albright_money:dashboard") + "?all=1",
        "overview": _overview(user) if everything else None})


def _overview(user):
    """"All my spaces": each space I see in Money with its accounts' balances and their total."""
    rows = _rows(list(_accounts(user).filter(archived=False).select_related("space__circle", "space__user")))
    out = []
    for s in money.visible_spaces(user):
        mine = [r for r in rows if r["account"].space_id == s.pk]
        out.append({"space": s, "rows": mine, "total": sum((r["balance"] for r in mine), ZERO)})
    return out


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------

def _filters(request):
    g = request.GET
    return {"account": g.get("account", ""), "category": g.get("category", ""), "month": g.get("month", ""),
            "review": g.get("review") == "1", "q": g.get("q", "").strip()[:100]}


@login_required
@never_cache
def transactions(request):
    user = request.user
    _owner, space = _scope(request)
    f = _filters(request)
    qs = Transaction.objects.visible_to(user).filter(account__space=space).select_related(
        "account", "category", "entered_by").prefetch_related("splits__category")
    if f["account"].isdigit():
        qs = qs.filter(account_id=int(f["account"]))
    if f["category"] == "none":
        qs = qs.filter(category__isnull=True, is_transfer=False).exclude(splits__isnull=False)
    elif f["category"].isdigit():
        qs = qs.filter(Q(category_id=int(f["category"])) | Q(splits__category_id=int(f["category"]))).distinct()
    if f["month"]:
        month = _month(f["month"], None)
        if month:
            qs = qs.filter(date__gte=month, date__lte=budgets.month_end(month))
    if f["review"]:
        qs = qs.filter(needs_review=True)
    if f["q"]:
        qs = qs.filter(Q(payee__icontains=f["q"]) | Q(notes__icontains=f["q"]) | Q(tags__icontains=f["q"]))
    rows = list(qs[:LIST_LIMIT + 1])
    groups = []
    for t in rows[:LIST_LIMIT]:
        if not groups or groups[-1]["date"] != t.date:
            groups.append({"date": t.date, "items": []})
        groups[-1]["items"].append(t)
    accounts = list(_accounts(user).filter(archived=False, space=space))
    by_scope = _categories_by_scope(user)
    offer = None
    if request.GET.get("offer", "").isdigit():
        offer = _rule_offer(user, Transaction.objects.visible_to(user).filter(pk=int(request.GET["offer"])).first())
    return render(request, "albright_money/transactions.html", {
        "groups": groups, "more": len(rows) > LIST_LIMIT, "filters": f, "accounts": accounts,
        "all_accounts": _accounts(user).filter(space=space), "categories": Category.objects.visible_to(user).filter(archived=False, space=space),
        "by_scope": by_scope, "offer": offer, "default_account": accounts[0].pk if accounts else None,
        "here": request.get_full_path(), "months": _recent_months()})


def _recent_months(n=12):
    month = budgets.this_month()
    return [budgets.add_months(month, -i) for i in range(n)]


def _categories_by_scope(user):
    """{space id: [categories]} - a row's category menu is its account's space's set."""
    return {s.pk: list(money.ensure_categories(user, s)) for s in money.visible_spaces(user)}


def _rule_offer(user, t):
    """"Always categorize <payee> as <category>?" - only if no rule already does that."""
    if not t or not t.payee.strip() or not t.category_id or t.is_transfer:
        return None
    rule = money.rule_for(t)
    if rule and rule.category_id == t.category_id:
        return None
    return {"transaction": t, "payee": money.rule_key(t.payee) or t.payee, "category": t.category}


@login_required
@require_POST
def quick_add(request):
    user = request.user
    account = get_object_or_404(_accounts(user).filter(archived=False), pk=request.POST.get("account") or 0)
    parsed = quickadd.parse(request.POST.get("text", ""), money.categories_for_account(account))
    if parsed is None:
        messages.error(request, "Start or end with the amount - e.g. “42.18 Publix groceries”.")
        return redirect(_next(request, "albright_money:transactions"))
    when = request.POST.get("date")
    try:
        when = date.fromisoformat(when) if when else timezone.localdate()
    except ValueError:
        when = timezone.localdate()
    t = Transaction(account=account, date=when, amount=parsed["amount"], payee=parsed["payee"],
                    category=parsed["category"], entered_by=user, source=Transaction.MANUAL)
    money.categorize(t)
    t.save()
    money.record_big_transaction(t, user)
    word = "in" if t.amount > 0 else "out"
    messages.success(request, f"Added ${abs(t.amount):,.2f} {word} – {t.payee or 'no payee'}"
                              + (f", {t.category.name}." if t.category else " (needs a category)."))
    return redirect(_next(request, "albright_money:transactions"))


@login_required
def transaction(request, pk):
    user = request.user
    t = get_object_or_404(Transaction.objects.visible_to(user).select_related("account"), pk=pk)
    form = TransactionForm(request.POST or None, instance=t, user=user)
    splits = None
    if request.method == "POST" and form.is_valid():
        account = form.cleaned_data["account"]
        splits = SplitRows(request.POST, account, form.cleaned_data["amount"])
        if not splits.errors:
            with db_transaction.atomic():
                t = form.save(commit=False)
                if splits.rows:
                    t.category = None                       # the parts carry the categories
                # Nothing categorizes it (and it isn't a transfer): it still needs a look.
                categorized = bool(t.category_id or splits.rows or t.is_transfer)
                t.needs_review = form.cleaned_data["needs_review"] or not categorized
                t.save()
                t.splits.all().delete()
                Split.objects.bulk_create([Split(transaction=t, **row) for row in splits.rows])
            messages.success(request, "Saved.")
            return redirect(_next(request, "albright_money:transactions"))
    existing = [{"category": s.category, "amount": s.amount, "note": s.note} for s in t.splits.all()]
    return render(request, "albright_money/transaction.html", {
        "t": t, "form": form, "split_errors": splits.errors if splits else [],
        "split_rows": existing + [None] * max(2, 3 - len(existing)), "by_scope": _categories_by_scope(user),
        "next": _next(request, "albright_money:transactions")})


@login_required
@require_POST
def transaction_delete(request, pk):
    t = get_object_or_404(Transaction.objects.visible_to(request.user), pk=pk)
    pair = t.transfer_pair
    money.forget_activity(t, pair)
    t.delete()
    if pair and Transaction.objects.visible_to(request.user).filter(pk=pair.pk).exists():
        pair.delete()                                   # a transfer goes as a whole
    messages.success(request, "Deleted.")
    return redirect(_next(request, "albright_money:transactions"))


@login_required
@require_POST
def set_category(request, pk):
    """The list's inline category menu: "" = uncategorized. Answers with the "Always categorize" offer."""
    user = request.user
    t = get_object_or_404(Transaction.objects.visible_to(user).select_related("account"), pk=pk)
    raw = request.POST.get("category", "")
    category = None
    if raw:
        category = get_object_or_404(money.categories_for_account(t.account), pk=raw if raw.isdigit() else 0)
    t.category = category
    t.needs_review = category is None and not t.is_transfer
    t.save(update_fields=["category", "needs_review"])
    t.splits.all().delete()
    offer = _rule_offer(user, t)
    if _wants_json(request):
        return JsonResponse({"ok": True, "needs_review": t.needs_review, "offer": offer and {
            "payee": offer["payee"], "category": offer["category"].name, "transaction": t.pk,
            "url": reverse("albright_money:always", args=[t.pk])}})
    target = _next(request, "albright_money:transactions")
    if offer:
        target += ("&" if "?" in target else "?") + f"offer={t.pk}"
    return redirect(target)


@login_required
@require_POST
def always(request, pk):
    """"Always categorize <payee> as <category>": a rule, applied to the others it matches now."""
    user = request.user
    t = get_object_or_404(Transaction.objects.visible_to(user).select_related("account", "category"), pk=pk)
    if not t.category:
        raise Http404
    _rule, changed = money.create_rule_from(t, t.category, user)
    text = f"From now on “{money.rule_key(t.payee)}” is {t.category.name}" + (
        f" – and {changed} more transaction{'s' if changed != 1 else ''} updated." if changed else ".")
    if _wants_json(request):
        return JsonResponse({"ok": True, "message": text, "changed": changed})
    messages.success(request, text)
    return redirect(_next(request, "albright_money:transactions"))


@login_required
def transfer(request):
    """Money moved between two of my accounts: one transaction out, one in - both marked transfers, so neither
    counts as spending or income."""
    form = TransferForm(request.POST or None, user=request.user)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        payee = d["note"] or f"Transfer: {d['from_account'].name} → {d['to_account'].name}"
        with db_transaction.atomic():
            out = Transaction.objects.create(account=d["from_account"], date=d["date"], amount=-d["amount"], payee=payee,
                                             is_transfer=True, entered_by=request.user)
            into = Transaction.objects.create(account=d["to_account"], date=d["date"], amount=d["amount"], payee=payee,
                                              is_transfer=True, entered_by=request.user, transfer_pair=out)
            out.transfer_pair = into
            out.save(update_fields=["transfer_pair"])
        messages.success(request, f"Moved ${d['amount']:,.2f} from {d['from_account'].name} to {d['to_account'].name}.")
        return redirect("albright_money:transactions")
    return render(request, "albright_money/transfer.html", {"form": form})


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------

def _rescope(account):
    """An account moved to another space: its transactions take the same-named categories in the new
    set (a name it doesn't have: uncategorized, needs review)."""
    by_name = {c.name.lower(): c for c in money.categories_for_account(account)}
    for t in account.transactions.select_related("category"):
        if t.category and not t.category.same_scope(account):
            t.category = by_name.get(t.category.name.lower())
            t.needs_review = t.category is None and not t.is_transfer
            t.save(update_fields=["category", "needs_review"])
    for s in Split.objects.filter(transaction__account=account).select_related("category"):
        if s.category and not s.category.same_scope(account):
            s.category = by_name.get(s.category.name.lower())
            s.save(update_fields=["category"])


def _save_account(form, request, account=None):
    account = form.save(commit=False)
    if account.pk is None:
        account.owner = request.user
    was = account.space_id
    account.space = form.cleaned_data["space"]
    account.save()
    if was is not None and was != account.space_id:
        money.forget_account_activity(account)          # its circle events go with it
    if was != account.space_id and account.transactions.exists():
        _rescope(account)
    return account


@login_required
def accounts(request):
    user = request.user
    _owner, space = _scope(request)
    form = AccountForm(request.POST or None, spaces=request.spaces, current=space)
    if request.method == "POST" and form.is_valid():
        account = _save_account(form, request)
        messages.success(request, f"Added {account.name} to {account.space.name}.")
        return redirect(_in(account.space, reverse("albright_money:accounts")))
    rows = _rows(list(_accounts(user).filter(space=space).select_related("space__circle", "owner")))
    return render(request, "albright_money/accounts.html", {"rows": rows, "form": form, "space": space,
                                                            "open_form": request.method == "POST" or not rows})


@login_required
def account(request, pk):
    user = request.user
    a = get_object_or_404(_accounts(user).select_related("space__circle"), pk=pk)
    space = a.space
    form = AccountForm(request.POST or None, instance=a, spaces=money.visible_spaces(user))
    if request.method == "POST" and form.is_valid():
        _save_account(form, request, a)
        messages.success(request, "Saved.")
        return redirect("albright_money:accounts")
    label, shown = money.shown_balance(a, money.balance(a))
    return render(request, "albright_money/account.html", {"a": a, "form": form, "label": label, "shown": shown,
                                                           "count": a.transactions.count(), "space": space})


# ---------------------------------------------------------------------------
# CSV import
# ---------------------------------------------------------------------------

@login_required
def import_csv(request):
    """Upload -> (map the columns, the first time for an account) -> preview -> import. The rows ride along in
    a signed field between steps; the file is never stored."""
    user = request.user
    step = request.POST.get("step", "upload")
    try:
        if request.method == "POST" and step == "upload":
            form = UploadForm(request.POST, request.FILES, user=user)
            if form.is_valid():
                account = form.cleaned_data["account"]
                headers, rows = imports.read(form.cleaned_data["file"])
                if imports.mapping_fits(account.csv_mapping, headers):
                    return _preview(request, account, headers, rows, account.csv_mapping)
                guess = imports.guess_mapping(headers)
                guess["style"] = "split" if guess.get("debit") and not guess.get("amount") else "amount"
                mapping = MappingForm(initial=guess, headers=headers)
                return _mapping_page(request, account, headers, rows, mapping)
            return render(request, "albright_money/import.html", {"step": "upload", "form": form})
        if request.method == "POST" and step == "map":
            data = imports.unpack(request.POST.get("payload"))
            account = get_object_or_404(_accounts(user), pk=data["account"])
            mapping = MappingForm(request.POST, headers=data["headers"])
            if not mapping.is_valid():
                return _mapping_page(request, account, data["headers"], data["rows"], mapping)
            account.csv_mapping = mapping.mapping()
            account.save(update_fields=["csv_mapping"])
            return _preview(request, account, data["headers"], data["rows"], account.csv_mapping)
        if request.method == "POST" and step == "remap":
            data = imports.unpack(request.POST.get("payload"))
            account = get_object_or_404(_accounts(user), pk=data["account"])
            initial = dict(account.csv_mapping)
            initial["style"] = "amount" if initial.get("amount") else "split"
            return _mapping_page(request, account, data["headers"], data["raw"],
                                 MappingForm(initial=initial, headers=data["headers"]))
        if request.method == "POST" and step == "import":
            data = imports.unpack(request.POST.get("payload"))
            account = get_object_or_404(_accounts(user), pk=data["account"])
            created, skipped = imports.import_rows(account, data["rows"], user)
            review = Transaction.objects.filter(account=account, needs_review=True).count()
            messages.success(request, f"Imported {created} transaction{'s' if created != 1 else ''} into {account.name}"
                                      + (f", skipped {skipped} already there" if skipped else "") + "."
                                      + (f" {review} need a category." if review else ""))
            return redirect(reverse("albright_money:transactions") + f"?account={account.pk}"
                            + ("&review=1" if review else ""))
    except imports.ImportProblem as problem:
        messages.error(request, str(problem))
        return redirect("albright_money:import")
    return render(request, "albright_money/import.html", {"step": "upload", "form": UploadForm(user=user)})


def _mapping_page(request, account, headers, rows, mapping):
    payload = imports.pack({"account": account.pk, "headers": headers, "rows": rows})
    return render(request, "albright_money/import.html", {
        "step": "map", "account": account, "headers": headers, "sample": rows[:3], "mapping": mapping,
        "payload": payload, "count": len(rows)})


def _preview(request, account, headers, rows, mapping):
    parsed, problems = imports.normalize(headers, rows, mapping)
    parsed = imports.preview(account, parsed)
    new = [r for r in parsed if not r["duplicate"]]
    return render(request, "albright_money/import.html", {
        "step": "preview", "account": account, "rows": parsed, "problems": problems[:20],
        "problem_count": len(problems), "new_count": len(new), "dup_count": len(parsed) - len(new),
        "payload": imports.pack({"account": account.pk, "rows": [{k: r[k] for k in ("date", "payee", "amount")}
                                                                 for r in parsed]}),
        "remap_payload": imports.pack({"account": account.pk, "headers": headers, "raw": rows})})


# ---------------------------------------------------------------------------
# Categories and rules
# ---------------------------------------------------------------------------

@login_required
def categories(request):
    user = request.user
    owner, space = _scope(request)
    if request.method == "POST":
        form = CategoryForm(request.POST)
        if form.is_valid():
            c = form.save(commit=False)
            c.owner, c.space = owner, space
            c.order = Category.objects.filter(money.scope_filter(owner, space)).count()
            c.save()
            messages.success(request, f"Added {c.name}.")
        else:
            messages.error(request, "Give the category a name.")
        return redirect(_in(space, reverse("albright_money:categories")))
    money.ensure_categories(owner, space)
    cats = Category.objects.filter(money.scope_filter(owner, space)).annotate(
        uses=Count("transactions", distinct=True) + Count("splits", distinct=True))
    rules = Rule.objects.filter(money.scope_filter(owner, space)).select_related("category", "account")
    return render(request, "albright_money/categories.html", {
        "cats": cats, "rules": rules, "space": space, "form": CategoryForm(), "rule_form": RuleForm(owner=owner, space=space)})


@login_required
@require_POST
def category(request, pk):
    c = get_object_or_404(Category.objects.visible_to(request.user), pk=pk)
    back = _in(c.space, reverse("albright_money:categories"))
    if request.POST.get("action") == "delete":
        if c.transactions.exists() or c.splits.exists():
            c.archived = True
            c.save(update_fields=["archived"])
            messages.success(request, f"{c.name} is in use, so it was archived instead (its history stays).")
        else:
            c.delete()
            messages.success(request, f"Deleted {c.name}.")
        return redirect(back)
    form = CategoryForm(request.POST, instance=c)
    if form.is_valid():
        form.save()
        messages.success(request, "Saved.")
    else:
        messages.error(request, "Give the category a name.")
    return redirect(back)


@login_required
@require_POST
def rules(request):
    owner, space = _scope(request)
    form = RuleForm(request.POST, owner=owner, space=space)
    back = _in(space, reverse("albright_money:categories"))
    if form.is_valid():
        rule = form.save(commit=False)
        rule.owner, rule.space = owner, space
        rule.payee_contains = rule.payee_contains.strip()
        rule.save()
        messages.success(request, f"Rule added: “{rule.payee_contains}” → {rule.category.name}.")
    else:
        messages.error(request, "That rule needs some payee text and a category.")
    return redirect(back)


@login_required
@require_POST
def rule_delete(request, pk):
    rule = get_object_or_404(Rule.objects.visible_to(request.user), pk=pk)
    back = _in(rule.space, reverse("albright_money:categories"))
    rule.delete()
    messages.success(request, "Rule deleted.")
    return redirect(back)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

@login_required
@never_cache
def budget(request):
    user = request.user
    owner, space = _scope(request)
    month = _month(request.POST.get("month") or request.GET.get("month"), budgets.this_month())
    here = _in(space, reverse("albright_money:budget") + f"?month={month:%Y-%m}")
    if request.method == "POST":
        if request.POST.get("action") == "copy":
            copied = budgets.copy_last_month(owner, space, month)
            if copied is None:
                messages.error(request, "Last month has no budget to copy.")
            else:
                messages.success(request, f"Copied {copied} amount{'s' if copied != 1 else ''} from last month.")
            return redirect(here)
        planned = {}
        for key, value in request.POST.items():
            if key.startswith("planned_") and key[8:].isdigit():
                amount = money.to_decimal(value or "0")
                if amount is None or amount < 0:
                    messages.error(request, "Planned amounts must be numbers of 0 or more.")
                    return redirect(here)
                planned[int(key[8:])] = amount
        rollover = {int(v) for v in request.POST.getlist("rollover") if v.isdigit()}
        cats = money.ensure_categories(owner, space)
        for c in cats:
            if c.kind == Category.EXPENSE and c.rollover != (c.pk in rollover):
                c.rollover = c.pk in rollover
                c.save(update_fields=["rollover"])
        budgets.save_plan(owner, space, month, planned)
        messages.success(request, "Budget saved.")
        return redirect(here)
    view = budgets.month_view(owner, space, month)
    return render(request, "albright_money/budget.html", {
        "v": view, "space": space, "prev": budgets.add_months(month, -1),
        "next": budgets.add_months(month, 1), "is_this_month": month == budgets.this_month()})


# ---------------------------------------------------------------------------
# The old Household page -> Circles
# ---------------------------------------------------------------------------

def household(request):
    """Who shares Money is now a circle (My account > Circles)."""
    return redirect("labs:circles")
