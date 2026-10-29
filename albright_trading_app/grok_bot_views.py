"""The Grok Bot page (/grok-bot/) and each strategy's detail page - READ-ONLY
views of the Alpaca paper account the bot strategies trade in. All Alpaca
access goes through grok_bot.ReadOnlyAlpaca, which can't place, modify or
cancel anything. The only things these views save are the user's own
strategy definitions (BotStrategy: name, prefixes, color, status, order).
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import strategy_attribution as sa
from .forms import BotStrategyForm
from .grok_bot import DEFAULT_PERIOD, PERIODS, RECENT_ORDERS, GrokBotError, build_dashboard, period_start
from .models import BOT_STRATEGY_COLORS, AlpacaCredentials, BotStrategy, StrategyDailySnapshot


def _period(request):
    return request.GET.get("period") if request.GET.get("period") in PERIODS else DEFAULT_PERIOD


def _strategies(user):
    strategies = list(BotStrategy.objects.filter(user=user))
    return strategies, sa.refs_from(strategies)


def _dashboard(request, refs, period):
    """(data, error message, not connected)."""
    try:
        credentials = request.user.alpaca_credentials
    except AlpacaCredentials.DoesNotExist:
        return None, None, True
    try:
        return build_dashboard(credentials, request.user.pk, refs, period=period), None, False
    except GrokBotError as exc:
        return None, str(exc), False


def _unix(day):
    return int(datetime(day.year, day.month, day.day, tzinfo=dt_timezone.utc).timestamp())


def _series(user, rows, period, today=None):
    """Each bucket's cumulative P&L per day over the period, for the overlay chart: the day's snapshot
    (manage.py snapshot_strategies) when there is one, else realized P&L rebuilt from the fills; today live."""
    today = today or timezone.localdate()
    start = period_start(period)
    start_day = timezone.localtime(start).date() if start else None
    snapshots = StrategyDailySnapshot.objects.filter(user=user)
    if start_day:
        snapshots = snapshots.filter(date__gte=start_day - timedelta(days=1))
    by_key = defaultdict(dict)
    for snap in snapshots:
        by_key[str(snap.strategy_id) if snap.strategy_id else sa.UNATTRIBUTED][snap.date] = snap.total_pnl
    series = []
    for row in rows:
        snaps = by_key.get(row["key"], {})
        if not row["fill_count"] and not snaps:
            continue
        if start_day:
            first, baseline = start_day, start_day - timedelta(days=1)
        else:
            days = [timezone.localtime(r.time).date() for r in row["realizations"] if r.time] + list(snaps)
            first, baseline = (min(days) if days else today), None
        points = sa.cumulative_series(row["realizations"], first, today, row["realized_to_date"] + row["unrealized"],
                                      snaps, baseline)
        series.append({"key": row["key"], "name": row["name"], "color": row["color"],
                       "points": [{"time": _unix(day), "value": float(value)} for day, value in points]})
    return series


def _next_color(strategies):
    used = {s.color for s in strategies}
    return next((value for value, _label in BOT_STRATEGY_COLORS if value not in used), BOT_STRATEGY_COLORS[0][0])


def _render_page(request, strategy_form=None, status=200):
    strategies, refs = _strategies(request.user)
    period = _period(request)
    strategy_form = strategy_form or BotStrategyForm(user=request.user, initial={
        "color": _next_color(strategies), "status": "active", "started_at": timezone.localdate()})
    context = {
        "period": period, "periods": [(key, spec["label"]) for key, spec in PERIODS.items()],
        "strategies": strategies, "warnings": sa.ambiguities(refs), "strategy_form": strategy_form,
        "unattributed_key": sa.UNATTRIBUTED, "filter": request.GET.get("strategy", ""),
        "not_connected": False, "error": None, "data": None, "account_points": [], "series": [],
    }
    data, context["error"], context["not_connected"] = _dashboard(request, refs, period)
    context["data"] = data
    if data:
        if data["history"]:
            context["account_points"] = data["history"]["points"]
        if data["report"]:
            rows = data["report"]["rows"] + [data["report"]["unattributed"]]
            context["series"] = _series(request.user, rows, period)
    return render(request, "grok_bot.html", context, status=status)


@login_required
def grok_bot(request):
    """Whole-account tiles, the strategy comparison table and cumulative P&L chart, the Strategies
    manager, and Open positions / Recent orders / Closed trades with a strategy filter."""
    return _render_page(request)


@login_required
def grok_bot_strategy(request, key):
    """One strategy's own page (or Unattributed): its tiles, equity curve, open positions from its fills,
    recent orders, closed trades and the bot notes tagged with it."""
    strategy = None
    if key != sa.UNATTRIBUTED:
        if not key.isdigit():
            raise Http404
        strategy = get_object_or_404(BotStrategy, pk=int(key), user=request.user)
    _strategies_list, refs = _strategies(request.user)
    period = _period(request)
    context = {"strategy": strategy, "key": key, "name": strategy.name if strategy else sa.UNATTRIBUTED_NAME,
               "color": strategy.color if strategy else sa.UNATTRIBUTED_COLOR, "period": period,
               "periods": [(k, spec["label"]) for k, spec in PERIODS.items()], "row": None, "orders": None,
               "series": [], "not_connected": False, "error": None, "data": None,
               "note_filter": strategy if strategy else "untagged"}
    data, context["error"], context["not_connected"] = _dashboard(request, refs, period)
    context["data"] = data
    if data:
        if data["orders"] is not None:
            context["orders"] = [o for o in data["orders"] if o["strategy"] == key][:RECENT_ORDERS]
        if data["report"]:
            row = data["report"]["by_key"][key]
            context["row"] = row
            context["series"] = _series(request.user, [row], period)
    return render(request, "grok_bot_strategy.html", context)


@login_required
@require_POST
def grok_bot_strategy_add(request):
    strategies = list(BotStrategy.objects.filter(user=request.user))
    form = BotStrategyForm(request.POST, user=request.user)
    if not form.is_valid():
        return _render_page(request, strategy_form=form, status=400)
    strategy = form.save(commit=False)
    strategy.display_order = max((s.display_order for s in strategies), default=-1) + 1
    strategy.save()
    messages.success(request, f"Added {strategy.name}. Run snapshot_strategies to build its history.")
    return redirect(reverse("grok_bot") + "#strategies")


@login_required
def grok_bot_strategy_edit(request, pk):
    strategy = get_object_or_404(BotStrategy, pk=pk, user=request.user)
    old_prefixes = list(strategy.prefixes)
    form = BotStrategyForm(request.POST or None, instance=strategy, user=request.user)
    if request.method == "POST" and form.is_valid():
        form.save()
        note = ""
        if [p.casefold() for p in strategy.prefixes] != [p.casefold() for p in old_prefixes]:
            note = " Its prefixes changed - run snapshot_strategies --rebuild to redo its saved history."
        messages.success(request, f"Saved {strategy.name}.{note}")
        return redirect(reverse("grok_bot") + "#strategies")
    _strategies_list, refs = _strategies(request.user)
    return render(request, "grok_bot_strategy_form.html", {"form": form, "strategy": strategy,
                                                           "warnings": sa.ambiguities(refs)})


@login_required
@require_POST
def grok_bot_strategy_status(request, pk):
    """Pause, retire or reactivate. A paused or retired strategy's orders are still attributed to it."""
    strategy = get_object_or_404(BotStrategy, pk=pk, user=request.user)
    status = request.POST.get("status")
    if status in dict(BotStrategy.STATUSES):
        strategy.status = status
        strategy.save(update_fields=["status", "updated_at"])
        messages.success(request, f"{strategy.name}: {strategy.get_status_display().lower()}.")
    return redirect(reverse("grok_bot") + "#strategies")


@login_required
@require_POST
def grok_bot_strategy_move(request, pk):
    """Moves a strategy up or down one place in the display order."""
    with transaction.atomic():
        strategies = list(BotStrategy.objects.select_for_update().filter(user=request.user))
        index = next((i for i, s in enumerate(strategies) if s.pk == pk), None)
        if index is None:
            raise Http404
        other = index - 1 if request.POST.get("direction") == "up" else index + 1
        if 0 <= other < len(strategies):
            strategies[index], strategies[other] = strategies[other], strategies[index]
        for order, strategy in enumerate(strategies):
            if strategy.display_order != order:
                strategy.display_order = order
                strategy.save(update_fields=["display_order", "updated_at"])
    return redirect(reverse("grok_bot") + "#strategies")
