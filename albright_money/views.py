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

from albright_labs import quick, sheet

from . import budget as budgets
from . import imports, money, quickadd
from .forms import AccountForm, CategoryForm, MappingForm, RuleForm, SplitRows, TransactionForm, TransferForm, UploadForm
from .models import ZERO, Account, BudgetDraft, BudgetPlanItem, BudgetPlanLine, Category, Rule, Split, Transaction

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
        if quick.wanted(request):
            return quick.error("Start or end with the amount - e.g. “42.18 Publix groceries”.")
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
    if quick.wanted(request):
        return quick.ok(f"Added ${abs(t.amount):,.2f} {word} – {t.payee or 'no payee'}",
                        reverse("albright_money:transaction", args=[t.pk]))
    messages.success(request, f"Added ${abs(t.amount):,.2f} {word} – {t.payee or 'no payee'}"
                              + (f", {t.category.name}." if t.category else " (needs a category)."))
    return redirect(_next(request, "albright_money:transactions"))


@login_required
def transaction(request, pk):
    user = request.user
    t = get_object_or_404(Transaction.objects.visible_to(user).select_related("account"), pk=pk)
    was_space_id = t.account.space_id                   # before is_valid() copies the new account onto t
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
                # Keep the other side of a transfer aligned (amount opposite, same date/payee).
                if t.transfer_pair_id:
                    pair = Transaction.objects.visible_to(user).filter(pk=t.transfer_pair_id).first()
                    if pair is not None:
                        pair.date = t.date
                        pair.amount = -t.amount
                        pair.payee = t.payee
                        pair.is_transfer = True
                        pair.save(update_fields=["date", "amount", "payee", "is_transfer"])
                        if not t.is_transfer:
                            t.is_transfer = True
                            t.save(update_fields=["is_transfer"])
                t.splits.all().delete()
                Split.objects.bulk_create([Split(transaction=t, **row) for row in splits.rows])
            if account.space_id != was_space_id:
                money.forget_activity(t)                # left the circle space: hide payee/amount at once
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
    if t.splits.exists():
        # The list's category menu is for simple rows; splits keep their own categories.
        if _wants_json(request):
            return JsonResponse(
                {"ok": False, "error": "Split transactions are categorized on the transaction page."},
                status=400)
        messages.error(request, "Split transactions are categorized on the transaction page.")
        return redirect(_next(request, "albright_money:transactions"))
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
        was = None
        account.space = form.cleaned_data["space"]
    else:
        was = account.space_id
        # Only the owner may move it. A circle member must not walk a household
        # account into their Personal space (or anywhere else) via this form.
        if account.owner_id == request.user.pk:
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
    form = AccountForm(request.POST or None, instance=a, spaces=money.visible_spaces(user), user=user)
    if request.method == "POST" and form.is_valid():
        _save_account(form, request, a)
        messages.success(request, "Saved.")
        return redirect("albright_money:accounts")
    label, shown = money.shown_balance(a, money.balance(a))
    return render(request, "albright_money/account.html", {"a": a, "form": form, "label": label, "shown": shown,
                                                           "count": a.transactions.count(), "space": space,
                                                           "share": sheet.sheet(user, a)})


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
    from_notes = budgets.notes_for_lines(user, view["budget"])
    for row in view["rows"] + view["income"]:
        row["from_notes"] = from_notes.get(row["category"].pk, [])
    drafts = list(budgets.open_drafts(space))
    from albright_notes.models import Note
    visible = set(Note.objects.visible_to(user).filter(pk__in=[d.note_id for d in drafts if d.note_id])
                  .values_list("pk", flat=True))
    for d in drafts:
        d.note_visible = d.note_id in visible
    return render(request, "albright_money/budget.html", {
        "v": view, "space": space, "prev": budgets.add_months(month, -1),
        "next": budgets.add_months(month, 1), "is_this_month": month == budgets.this_month(),
        "drafts": drafts, "draft_categories": [c for c in money.ensure_categories(owner, space) if not c.is_income],
        "here": here})


@login_required
@require_POST
def budget_draft(request, pk):
    """A draft from a note (Budget page, "From notes"): action=confirm puts its amount on the month's budget for the
    picked category (or a new one named after it); action=dismiss deletes it (the note's chip then shows it gone)."""
    draft = get_object_or_404(BudgetDraft.objects.visible_to(request.user), pk=pk, confirmed_at__isnull=True)
    month = _month(request.POST.get("month"), budgets.this_month())
    back = _next(request, "albright_money:budget")
    if request.POST.get("action") == "dismiss":
        draft.delete()
        messages.success(request, f"Dismissed “{draft.name}”.")
        return redirect(back)
    amount = money.to_decimal(request.POST.get("planned") or "")
    if amount is None or amount <= 0:
        messages.error(request, f"Give “{draft.name}” a planned amount over $0.")
        return redirect(back)
    category, new_name = None, ""
    picked = request.POST.get("category") or "new"
    if picked.isdigit():
        category = Category.objects.filter(space=draft.space, pk=int(picked), archived=False,
                                           kind=Category.EXPENSE).first()
        if category is None:
            messages.error(request, "Pick a spending category.")
            return redirect(back)
    else:
        new_name = request.POST.get("new_name") or draft.name
    line = budgets.confirm_draft(draft, request.user, month, amount, category=category, new_name=new_name)
    messages.success(request, f"Added {amount:,.2f} to {line.category.name} for {month:%B}.")
    return redirect(back)


# ---------------------------------------------------------------------------
# Budget plans (BudgetDraft + BudgetPlanLine)
# ---------------------------------------------------------------------------

def _plan(request, pk):
    """The plan `request.user` can see, or 404. Sets the page's space to the plan's space."""
    plan = budgets.visible_plan(request.user, pk)
    if plan is None:
        raise Http404
    request.spaces = money.visible_spaces(request.user)
    request.space = plan.space
    return plan


def _save_plan_lines(request, plan, here):
    """Update every posted line (name, planned, status, notes) and any posted items (label, amount).
    A posted spent field is applied only when the group has no items or one unlabeled item (older form)."""
    allowed = {line.pk: line for line in plan.plan_lines.prefetch_related("items").all()}
    errors = 0
    for line in allowed.values():
        prefix = f"line_{line.pk}_"
        name = (request.POST.get(prefix + "name") or "").strip()[:80]
        planned = money.to_decimal(request.POST.get(prefix + "planned") or "0")
        status = request.POST.get(prefix + "status") or BudgetPlanLine.TO_BUY
        notes = (request.POST.get(prefix + "notes") or "").strip()[:200]
        if not name or planned is None or planned < 0:
            errors += 1
            continue
        if status not in dict(BudgetPlanLine.STATUSES):
            status = line.status
        line.name, line.planned, line.status, line.notes = name, planned, status, notes
        line.planned_auto = False
        line.save()
        if prefix + "spent" in request.POST:
            spent = money.to_decimal(request.POST.get(prefix + "spent") or "0")
            if spent is None or spent < 0:
                errors += 1
                continue
            budgets.set_spent(line, spent)
        for item in line.items.all():
            ip = f"item_{item.pk}_"
            if ip + "label" not in request.POST and ip + "amount" not in request.POST:
                continue
            label = request.POST.get(ip + "label")
            raw = request.POST.get(ip + "amount")
            amount = money.to_decimal(raw or "0") if raw is not None else item.amount
            if amount is None or amount < 0:
                errors += 1
                continue
            budgets.update_item(item, amount=amount, label=None if label is None else label)
    if errors:
        messages.error(request, "Every line needs a name and amounts of 0 or more.")
        return redirect(here)
    messages.success(request, "Plan saved.")
    return redirect(here)


@login_required
@never_cache
def budget_plans(request):
    """Money > Budget > Plans: the space's plans, and a form to start a new one from a template."""
    user, space = _scope(request)
    here = _in(space, reverse("albright_money:budget_plans"))
    if request.method == "POST" and request.POST.get("action") == "create":
        posted = request.POST.get("space")
        if posted:
            try:
                sid = int(posted)
            except (TypeError, ValueError):
                raise Http404
            if sid not in {s.pk for s in request.spaces}:
                raise Http404
            space = next(s for s in request.spaces if s.pk == sid)
            here = _in(space, reverse("albright_money:budget_plans"))
        name = (request.POST.get("name") or "").strip()[:60]
        if not name:
            messages.error(request, "Give the plan a name.")
            return redirect(here)
        template = request.POST.get("template") or BudgetDraft.BLANK
        due = request.POST.get("due_date") or ""
        try:
            due_date = date.fromisoformat(due) if due else None
        except ValueError:
            messages.error(request, "Due date must be a real date.")
            return redirect(here)
        plan = budgets.create_plan(user, name, template=template, due_date=due_date, space=space)
        messages.success(request, f"Started “{plan.name}”.")
        return redirect(budgets.plan_url(plan))
    rows = []
    for plan in budgets.plans_for(space):
        rows.append({"plan": plan, **budgets.plan_totals(plan)})
    return render(request, "albright_money/plans.html", {
        "space": space, "plans": rows, "templates": BudgetDraft.TEMPLATES,
        "tab": "plans", "month": budgets.this_month(), "here": here})


@login_required
@never_cache
def budget_plan(request, pk):
    """One plan: pick a template (if it has no spend yet), later also edit lines and reserve in a month."""
    plan = _plan(request, pk)
    space = plan.space
    here = budgets.plan_url(plan)
    if request.method == "POST":
        action = request.POST.get("action") or ""
        if action == "apply_template":
            added = budgets.apply_template(plan, request.POST.get("template") or "", replace=True)
            if added is None:
                messages.error(request, "This plan already has spending on a line, so its template was left alone.")
            else:
                label = plan.get_template_display()
                messages.success(request, f"Applied the {label} template." if plan.template != BudgetDraft.BLANK
                                 else "Cleared the starter lines.")
            return redirect(here)
        if action == "save_meta":
            name = (request.POST.get("name") or "").strip()[:60]
            if not name:
                messages.error(request, "Give the plan a name.")
                return redirect(here)
            due = request.POST.get("due_date") or ""
            try:
                plan.due_date = date.fromisoformat(due) if due else None
            except ValueError:
                messages.error(request, "Due date must be a real date.")
                return redirect(here)
            cap = request.POST.get("planned")
            if cap not in (None, ""):
                amount = money.to_decimal(cap)
                if amount is None or amount < 0:
                    messages.error(request, "The cap must be a number of 0 or more.")
                    return redirect(here)
                plan.planned = amount or None
            else:
                plan.planned = None
            plan.name = name
            plan.save(update_fields=["name", "due_date", "planned"])
            messages.success(request, "Plan saved.")
            return redirect(here)
        if action == "save_lines":
            return _save_plan_lines(request, plan, here)
        if action == "add_line":
            name = (request.POST.get("name") or "").strip()[:80] or "Person or event"
            planned = money.to_decimal(request.POST.get("planned") or "0") or ZERO
            spent = money.to_decimal(request.POST.get("spent") or "0") or ZERO
            if planned < 0 or spent < 0:
                messages.error(request, "Amounts must be 0 or more.")
                return redirect(here)
            order = (plan.plan_lines.order_by("-order").values_list("order", flat=True).first() or 0) + 1
            line = BudgetPlanLine.objects.create(plan=plan, name=name, planned=planned, spent=ZERO,
                                                planned_auto=(planned == ZERO), order=order)
            if spent > 0:
                budgets.set_spent(line, spent)
            messages.success(request, "Line added.")
            return redirect(here)
        if action == "add_item":
            line = plan.plan_lines.filter(pk=request.POST.get("line")).first()
            if line is None:
                messages.error(request, "That person or event is gone.")
                return redirect(here)
            amount = money.to_decimal(request.POST.get("amount") or "0") or ZERO
            if amount <= 0:
                messages.error(request, "Give the item an amount over $0.")
                return redirect(here)
            label = (request.POST.get("label") or "").strip()[:80]
            budgets.add_item(plan, line.name, amount, label)
            messages.success(request, "Item added.")
            return redirect(here)
        if action == "delete_item":
            item = BudgetPlanItem.objects.filter(pk=request.POST.get("item"), line__plan=plan).first()
            if item is None:
                messages.error(request, "That item is gone.")
                return redirect(here)
            budgets.delete_item(item)
            messages.success(request, "Item removed.")
            return redirect(here)
        if action == "delete_line":
            line = plan.plan_lines.filter(pk=request.POST.get("line")).first()
            if line is None:
                messages.error(request, "That line is gone.")
                return redirect(here)
            line.delete()
            messages.success(request, "Line removed.")
            return redirect(here)
        if action == "reserve":
            amount = money.to_decimal(request.POST.get("planned") or "")
            if amount is None or amount <= 0:
                messages.error(request, "Give a planned amount over $0 to reserve.")
                return redirect(here)
            month = _month(request.POST.get("month"), budgets.this_month())
            category, new_name = None, ""
            picked = request.POST.get("category") or "new"
            if picked.isdigit():
                category = Category.objects.filter(space=space, pk=int(picked), archived=False,
                                                   kind=Category.EXPENSE).first()
                if category is None:
                    messages.error(request, "Pick a spending category.")
                    return redirect(here)
            else:
                new_name = request.POST.get("new_name") or plan.name
            line = budgets.reserve_plan(plan, request.user, month, amount, category=category, new_name=new_name)
            messages.success(request, f"Reserved {amount:,.2f} in {line.category.name} for {month:%B}.")
            return redirect(here)
        messages.error(request, "Nothing to do.")
        return redirect(here)
    totals = budgets.plan_totals(plan)
    note_visible = False
    if plan.note_id:
        from albright_notes.models import Note
        note_visible = Note.objects.visible_to(request.user).filter(pk=plan.note_id).exists()
    reserve_categories = [c for c in money.ensure_categories(request.user, space) if not c.is_income]
    this = budgets.this_month()
    return render(request, "albright_money/plan.html", {
        "plan": plan, "space": space, "tab": "plans", "month": this,
        "templates": BudgetDraft.TEMPLATES, "note_visible": note_visible,
        "reserve_categories": reserve_categories,
        "reserve_months": [this, budgets.add_months(this, 1)],
        "statuses": BudgetPlanLine.STATUSES,
        "share": sheet.sheet(request.user, plan),
        "here": here, **totals})


# ---------------------------------------------------------------------------
# The old Household page -> Circles
# ---------------------------------------------------------------------------

def household(request):
    """Who shares Money is now a circle (My account > Circles)."""
    return redirect("labs:circles")
