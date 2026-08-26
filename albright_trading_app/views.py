from django.shortcuts import render, redirect, get_object_or_404
from albright_trading_app.forms import UserForm, LoginForm, InvestorProfileForm
from django.urls import reverse
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect,HttpResponse
from django.contrib.auth import authenticate,login,logout
from django.contrib.auth.models import auth
from django.core.cache import cache
from django.views.decorators.http import require_POST
from .models import RedditDailyMentionCount, RedditSentimentSummary, Strategy, Trade, AlpacaCredentials, POSITION_SIZING_CHOICES
import requests
import json
import pandas as pd
import numpy as np
import datetime
import time
import logging
import datetime as dt
from dateutil import tz
from django.utils import timezone
from time import strftime, localtime
import base64
import ast
import io
from django.db.models import Sum
from django.core.mail import send_mail
from openai import OpenAI
from itertools import islice
from alpaca.trading.client import TradingClient
from alpaca.common.exceptions import APIError
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from django.http import JsonResponse
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

logger = logging.getLogger(__name__)

# Create your views here.
def index(request):
    return HttpResponse("Albright Trading")

def test(request):
    return HttpResponse("test")

SESSION_STATE_LABELS = {
    "open": "Market Open",
    "pre_market": "Pre-Market",
    "after_hours": "After Hours",
    "weekend": "Market Closed",
}
 
 
def get_market_session_state():
    """
    Returns one of: "open", "pre_market", "after_hours", "weekend".
    Derived from Alpaca's own trading clock, so holidays are handled
    correctly too, not just Saturday/Sunday. Used only for labeling
    now — the underlying data source is the same for every "closed"
    state (see get_last_session_snapshot_rows below).
    """
    trading_client = TradingClient(
        settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY, paper=settings.ALPACA_PAPER
    )
    clock = trading_client.get_clock()
 
    if clock.is_open:
        return "open"
 
    now = clock.timestamp
    next_open = clock.next_open
 
    if next_open.date() == now.date():
        return "pre_market"
    if (next_open.date() - now.date()).days == 1:
        return "after_hours"
    return "weekend"  # weekend or holiday — both treated the same way
 
 
def get_last_session_snapshot_rows(constituents):
    """
    Same row shape as get_snapshot_rows, but computed from the two
    most recently COMPLETED daily bars rather than the live snapshot.
    Used any time the market is closed — pre-market, after-hours,
    weekend, or holiday — so the numbers always describe how the
    last full trading day actually performed, rather than relying on
    live/extended-hours tick data that isn't reliably available on
    the free data feed. This is deliberately unambiguous: "the last
    two completed sessions" means the same thing regardless of which
    closed-state we're in.
    """
    symbol_lookup = {c["symbol"]: c for c in constituents}
    symbols = list(symbol_lookup.keys())
 
    client = StockHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )
 
    rows = []
    chunk_size = 100
    # 10 days back safely spans a long weekend or multi-day holiday
    # closure while still guaranteeing at least 2 completed bars.
    lookback_start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
 
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
        try:
            bars_by_symbol = client.get_stock_bars(
                StockBarsRequest(
                    symbol_or_symbols=chunk,
                    timeframe=TimeFrame.Day,
                    start=lookback_start,
                )
            ).data
        except Exception:
            logger.exception("Failed to fetch last-session bars for chunk starting at %d", i)
            continue
 
        for symbol in chunk:
            bars = bars_by_symbol.get(symbol, [])
            if len(bars) < 2:
                continue  # not enough history to compute a change yet
 
            latest = bars[-1]
            previous = bars[-2]
 
            price = float(latest.close)
            prev_close = float(previous.close)
            change = price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
 
            meta = symbol_lookup[symbol]
            rows.append({
                "symbol": symbol,
                "name": meta["name"],
                "sector": meta["sector"],
                "price": price,
                "change": change,
                "change_pct": change_pct,
                "volume": int(latest.volume),
                "day_high": float(latest.high),
                "day_low": float(latest.low),
                "is_gain": change >= 0,
                "session_date": latest.timestamp.date(),
            })
 
    return rows
 
 
def get_market_outlook():
    """
    Returns {"summary": {...}, "outlook_text": str or None}, or None
    if data couldn't be loaded. "open" uses live snapshot data; every
    other state uses the last two completed daily bars. Cached per
    session state so a state transition busts the cache immediately.
    """
    session_state = get_market_session_state()
    cache_key = f"market_outlook_{session_state}"
 
    cached = cache.get(cache_key)
    if cached:
        return cached
 
    try:
        constituents = get_sp500_constituents()
    except Exception:
        logger.exception("Failed to load constituents for home page outlook")
        return None
 
    try:
        if session_state == "open":
            rows = get_snapshot_rows(constituents)
        else:
            rows = get_last_session_snapshot_rows(constituents)
    except Exception:
        logger.exception(
            "Failed to load market data for home page outlook (state=%s)", session_state
        )
        return None
 
    if not rows:
        return None
 
    gainers = sorted(rows, key=lambda r: r["change_pct"], reverse=True)
    losers = sorted(rows, key=lambda r: r["change_pct"])
 
    advancers = sum(1 for r in rows if r["change_pct"] > 0)
    decliners = sum(1 for r in rows if r["change_pct"] < 0)
    unchanged = len(rows) - advancers - decliners
    avg_change_pct = sum(r["change_pct"] for r in rows) / len(rows)
 
    session_date = rows[0].get("session_date") if session_state != "open" else None
 
    summary = {
        "session_state": session_state,
        "session_state_label": SESSION_STATE_LABELS.get(session_state, session_state),
        "session_date": session_date,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "avg_change_pct": avg_change_pct,
        "top_gainers": gainers[:5],
        "top_losers": losers[:5],
        "generated_at": timezone.now(),
    }
 
    result = {"summary": summary, "outlook_text": _generate_ai_outlook(summary)}
 
    ttl = 60 * 10 if session_state == "open" else 60 * 30
    cache.set(cache_key, result, ttl)
    return result
 
 
def _generate_ai_outlook(summary):
    """
    Returns a short AI-generated market status paragraph, tailored to
    the current session state, or None if generation fails — the
    page falls back to showing just the numbers.
    """
    if not settings.OPENAI_API_KEY:
        return None
 
    state = summary["session_state"]
 
    gainers_lines = "\n".join(
        f"- {g['symbol']} ({g['name']}): {g['change_pct']:+.2f}%" for g in summary["top_gainers"]
    )
    losers_lines = "\n".join(
        f"- {l['symbol']} ({l['name']}): {l['change_pct']:+.2f}%" for l in summary["top_losers"]
    )
 
    breadth_line = (
        f"{summary['advancers']} advancers, {summary['decliners']} decliners, "
        f"{summary['unchanged']} unchanged. Average change: {summary['avg_change_pct']:.2f}%."
    )
 
    session_date_str = (
        f"{summary['session_date'].strftime('%A, %B')} {summary['session_date'].day}"
        if summary.get("session_date") else None
    )
 
    if state == "open":
        context_line = "The market is OPEN right now — this reflects live, in-progress trading today."
    elif state == "pre_market":
        context_line = (
            f"The market has not opened yet today. These numbers are from the last completed "
            f"trading session ({session_date_str}), NOT live pre-market activity — describe this "
            f"as the most recent close, not today's action, since today hasn't started trading yet."
        )
    elif state == "after_hours":
        context_line = (
            f"The market has closed for the day. These are the official closing numbers from "
            f"today's session ({session_date_str})."
        )
    else:  # weekend or holiday
        context_line = (
            f"Markets are currently closed (weekend or holiday). These numbers reflect how the "
            f"last trading session ({session_date_str}) closed. Frame this as a recap of how that "
            f"session finished, and simply note that trading resumes next session — do not predict "
            f"what will happen then."
        )
 
    prompt = f"""You are writing a short, factual market status update for a personal trading dashboard.
 
{context_line}
 
S&P 500 breadth: {breadth_line}
 
Top 5 gainers:
{gainers_lines}
 
Top 5 losers:
{losers_lines}
 
Write 3-4 sentences in a clear, professional tone matching the market status described above. Mention overall breadth/sentiment and name one or two notable movers. Do not give investment advice, recommendations, or predictions about future price direction — describe what happened, not what anyone should do about it."""
 
    try:
        client = OpenAI(api_key=settings.OPENAI_API_KEY)
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=220,
            temperature=0.6,
        )
        return response.choices[0].message.content.strip()
    except Exception:
        logger.exception("Failed to generate AI market outlook")
        return None
 
 
def home(request):
    outlook = get_market_outlook()
    context = {"outlook": outlook}
    return render(request, 'home.html', context=context)

@login_required
def connect_alpaca_account(request):
    existing = AlpacaCredentials.objects.filter(user=request.user).first()
 
    if request.method == "POST":
        api_key = request.POST.get("api_key", "").strip()
        secret_key = request.POST.get("secret_key", "").strip()
        is_paper = request.POST.get("is_paper") == "on"
 
        if not api_key or not secret_key:
            return render(request, "connect_alpaca_account.html", {
                "existing": existing,
                "error": "Both the API key and secret key are required.",
            })
 
        # Verify the credentials actually work before saving them — a
        # typo should surface immediately here, not silently break the
        # dashboard later.
        try:
            TradingClient(api_key, secret_key, paper=is_paper).get_account()
        except Exception:
            return render(request, "connect_alpaca_account.html", {
                "existing": existing,
                "error": (
                    "Couldn't verify those credentials with Alpaca. Double-check "
                    "the key, secret, and whether this is a paper or live account."
                ),
            })
 
        credentials, _ = AlpacaCredentials.objects.get_or_create(user=request.user)
        credentials.set_credentials(api_key, secret_key)
        credentials.is_paper = is_paper
        credentials.save()
 
        return redirect("trading_account")
 
    return render(request, "connect_alpaca_account.html", {"existing": existing, "error": None})
 
 
@login_required
def disconnect_alpaca_account(request):
    if request.method == "POST":
        AlpacaCredentials.objects.filter(user=request.user).delete()
    return redirect("connect_alpaca_account")
 
 
# ============================================================
# REPLACE your existing `dashboard` view with this version.
# Only the credential lookup at the top changed — everything
# below it (account/positions fetching, context building) is
# unchanged from what you already have.
# ============================================================
 
@login_required
def trading_account(request):
    try:
        credentials = request.user.alpaca_credentials
    except AlpacaCredentials.DoesNotExist:
        return render(request, "trading_account.html", {"error": None, "not_connected": True})
 
    client = TradingClient(
        api_key=credentials.get_api_key(),
        secret_key=credentials.get_secret_key(),
        paper=credentials.is_paper,
    )
 
    context = {"error": None, "not_connected": False}
 
    try:
        account = client.get_account()
        positions = client.get_all_positions()
    except Exception:
        context["error"] = "Couldn't reach Alpaca with your saved credentials. They may have been revoked or changed — try reconnecting your account."
        return render(request, "trading_account.html", context)
 
    equity = float(account.equity)
    last_equity = float(account.last_equity)
    day_change = equity - last_equity
    day_change_pct = (day_change / last_equity * 100) if last_equity else 0
 
    holdings = []
    for p in positions:
        unrealized_pl = float(p.unrealized_pl)
        holdings.append({
            "symbol": p.symbol,
            "qty": p.qty,
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price),
            "market_value": float(p.market_value),
            "unrealized_pl": unrealized_pl,
            "unrealized_plpc": float(p.unrealized_plpc) * 100,
            "is_gain": unrealized_pl >= 0,
        })
 
    holdings.sort(key=lambda h: h["market_value"], reverse=True)
 
    context.update({
        "equity": equity,
        "cash": float(account.cash),
        "buying_power": float(account.buying_power),
        "portfolio_value": float(account.portfolio_value),
        "day_change": day_change,
        "day_change_pct": day_change_pct,
        "is_gain_today": day_change >= 0,
        "holdings": holdings,
        "account_status": account.status,
        "is_paper": credentials.is_paper,
    })
 
    return render(request, "trading_account.html", context)

def _describe_entry_signal(strategy):
    p = strategy.parameters or {}
    t = strategy.strategy_type
 
    if t == "moving_average_crossover":
        return f"MA Crossover — {p.get('short_period', '?')}/{p.get('long_period', '?')} day"
 
    if t == "reddit_sentiment_threshold":
        parts = [
            f"\u2265{p.get('min_mentions_24h', '?')} mentions/24h",
            f"\u2265{p.get('min_positive_ratio_24h', 0) * 100:.0f}% positive",
        ]
        if p.get("min_positive_acceleration_pct") is not None:
            parts.append(f"+{p['min_positive_acceleration_pct']:.0f}% accel")
        return "Reddit Sentiment — " + ", ".join(parts)
 
    if t == "rsi_threshold":
        return (
            f"RSI({p.get('rsi_period', '?')}) — buy <{p.get('oversold_threshold', '?')}, "
            f"sell >{p.get('overbought_threshold', '?')}"
        )
 
    if t == "bollinger_reversion":
        return f"Bollinger({p.get('period', '?')}, {p.get('std_dev', '?')}\u03c3)"
 
    if t == "price_breakout":
        return f"{p.get('breakout_period', '?')}-Day Breakout"
 
    return strategy.get_strategy_type_display()
 
 
def _describe_universe(strategy):
    if strategy.symbols.strip():
        return strategy.symbols
 
    parts = []
    if strategy.filter_sectors_list:
        parts.append(", ".join(strategy.filter_sectors_list))
 
    if strategy.filter_min_price is not None or strategy.filter_max_price is not None:
        lo = f"${strategy.filter_min_price:.0f}" if strategy.filter_min_price is not None else "any"
        hi = f"${strategy.filter_max_price:.0f}" if strategy.filter_max_price is not None else "any"
        parts.append(f"Price {lo}\u2013{hi}")
 
    if strategy.filter_min_day_change_pct is not None:
        parts.append(f"Day change \u2265{strategy.filter_min_day_change_pct:.1f}%")
    if strategy.filter_max_day_change_pct is not None:
        parts.append(f"\u2264{strategy.filter_max_day_change_pct:.1f}%")
 
    if strategy.filter_min_reddit_mentions_24h is not None:
        parts.append(f"\u2265{strategy.filter_min_reddit_mentions_24h} Reddit mentions")
    if strategy.filter_min_reddit_positive_ratio is not None:
        parts.append(f"\u2265{strategy.filter_min_reddit_positive_ratio * 100:.0f}% positive (vs total)")
    if strategy.filter_min_reddit_positive_vs_negative_ratio is not None:
        parts.append(f"\u2265{strategy.filter_min_reddit_positive_vs_negative_ratio * 100:.0f}% positive (vs negative)")
 
    return " \u00b7 ".join(parts) if parts else "All S&P 500 stocks"
 
 
def _describe_sizing(strategy):
    method = strategy.position_sizing_method
    value = strategy.position_sizing_value
 
    if method == "fixed_shares":
        return f"{int(value)} share{'s' if value != 1 else ''} per trade"
    if method == "fixed_dollar":
        return f"${value:,.0f} per trade"
    if method == "pct_buying_power":
        return f"{value:.1f}% of buying power"
    if method == "pct_cash":
        return f"{value:.1f}% of cash"
    return f"{value} ({method})"
 
 
def _describe_risk_management(strategy):
    parts = []
    if strategy.take_profit_pct is not None:
        parts.append(f"TP {strategy.take_profit_pct:.1f}%")
    if strategy.stop_loss_pct is not None:
        parts.append(f"SL {strategy.stop_loss_pct:.1f}%")
    if strategy.trailing_stop_pct is not None:
        parts.append(f"Trailing {strategy.trailing_stop_pct:.1f}%")
    if strategy.max_hold_days is not None:
        parts.append(f"Max hold {strategy.max_hold_days}d")
    if strategy.max_daily_entries_per_symbol is not None:
        n = strategy.max_daily_entries_per_symbol
        parts.append(f"Max {n} entr{'y' if n == 1 else 'ies'}/symbol/day")
 
    return " \u00b7 ".join(parts) if parts else "No exit limits set"
 
 
def _build_strategy_rows(queryset):
    strategies_list = list(queryset)
 
    # One batched price fetch covering every open position across ALL
    # these strategies, rather than a separate Alpaca call per
    # strategy — stays fast regardless of how many strategies exist.
    all_open_trades = Trade.objects.filter(strategy__in=strategies_list, status="open")
    current_prices = get_current_prices([t.symbol for t in all_open_trades])
 
    open_trades_by_strategy = {}
    for trade in all_open_trades:
        open_trades_by_strategy.setdefault(trade.strategy_id, []).append(trade)
 
    strategy_rows = []
    for strategy in strategies_list:
        total_pnl = strategy.trades.filter(status="closed").aggregate(
            total=Sum("realized_pnl")
        )["total"] or 0
        closed_trades = strategy.trades.filter(status="closed").count()
        winning_trades = strategy.trades.filter(status="closed", realized_pnl__gt=0).count()
        win_rate = (winning_trades / closed_trades * 100) if closed_trades else None
 
        open_trades_for_strategy = open_trades_by_strategy.get(strategy.id, [])
        total_unrealized_pnl = 0
        for trade in open_trades_for_strategy:
            current_price = current_prices.get(trade.symbol)
            if current_price is not None and trade.entry_price:
                total_unrealized_pnl += (current_price - trade.entry_price) * trade.quantity
 
        strategy_rows.append({
            "strategy": strategy,
            "total_pnl": total_pnl,
            "total_unrealized_pnl": total_unrealized_pnl,
            "open_trades": len(open_trades_for_strategy),
            "closed_trades": closed_trades,
            "win_rate": win_rate,
            "is_gain": total_pnl >= 0,
            "is_unrealized_gain": total_unrealized_pnl >= 0,
            "entry_signal_desc": _describe_entry_signal(strategy),
            "universe_desc": _describe_universe(strategy),
            "sizing_desc": _describe_sizing(strategy),
            "risk_desc": _describe_risk_management(strategy),
        })
    return strategy_rows
 
 
@login_required
def strategies(request):
    user_strategies = Strategy.objects.filter(
        user=request.user, is_archived=False
    ).order_by("name")
 
    archived_count = Strategy.objects.filter(user=request.user, is_archived=True).count()
 
    context = {
        "strategy_rows": _build_strategy_rows(user_strategies),
        "archived_count": archived_count,
    }
    return render(request, 'strategies.html', context)
 
 
@login_required
def archived_strategies(request):
    user_strategies = Strategy.objects.filter(
        user=request.user, is_archived=True
    ).order_by("-updated_at")
 
    context = {"strategy_rows": _build_strategy_rows(user_strategies)}
    return render(request, 'archived_strategies.html', context)
 
 
@login_required
@require_POST
def strategy_archive(request, strategy_id):
    strategy = get_object_or_404(Strategy, id=strategy_id, user=request.user)
    strategy.is_archived = True
    strategy.is_active = False  # never leave an archived strategy silently trading
    strategy.save(update_fields=["is_archived", "is_active"])
    return redirect("strategies")
 
 
@login_required
@require_POST
def strategy_unarchive(request, strategy_id):
    strategy = get_object_or_404(Strategy, id=strategy_id, user=request.user)
    strategy.is_archived = False
    # Deliberately NOT re-activating — restoring a strategy shouldn't
    # silently put it back to trading. You toggle it on yourself once
    # you're ready.
    strategy.save(update_fields=["is_archived"])
    return redirect("archived_strategies")

@login_required
def strategies_list(request):
    strategies = Strategy.objects.filter(user=request.user).order_by("name")
 
    strategy_rows = []
    for strategy in strategies:
        total_pnl = strategy.trades.filter(status="closed").aggregate(
            total=Sum("realized_pnl")
        )["total"] or 0
        open_trades = strategy.trades.filter(status="open").count()
        closed_trades = strategy.trades.filter(status="closed").count()
        winning_trades = strategy.trades.filter(status="closed", realized_pnl__gt=0).count()
        win_rate = (winning_trades / closed_trades * 100) if closed_trades else None
 
        strategy_rows.append({
            "strategy": strategy,
            "total_pnl": total_pnl,
            "open_trades": open_trades,
            "closed_trades": closed_trades,
            "win_rate": win_rate,
            "is_gain": total_pnl >= 0,
        })
 
    return render(request, "strategies.html", {"strategy_rows": strategy_rows})

def get_current_prices(symbols):
    """
    Returns {symbol: latest_price} for an arbitrary list of symbols.
    Unlike get_snapshot_rows (built for the full S&P 500), this
    takes any symbol list — used here for whatever a strategy
    currently has open, which could be any size from 0 to many.
    """
    if not symbols:
        return {}
 
    client = StockHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )
 
    try:
        snapshots = client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=list(set(symbols)))
        )
    except Exception:
        logger.exception("Failed to fetch current prices for open trades")
        return {}
 
    prices = {}
    for symbol, snap in snapshots.items():
        if snap is None or snap.daily_bar is None:
            continue
        prices[symbol] = float(snap.daily_bar.close)
 
    return prices

 
@login_required
def strategy_detail(request, strategy_id):
    strategy = get_object_or_404(Strategy, id=strategy_id, user=request.user)
 
    open_trades_qs = strategy.trades.filter(status="open").order_by("-entered_at")
    closed_trades_qs = strategy.trades.filter(status="closed").order_by("-exited_at")
 
    current_prices = get_current_prices([t.symbol for t in open_trades_qs])
 
    open_positions = []
    total_unrealized_pnl = 0
    for trade in open_trades_qs:
        current_price = current_prices.get(trade.symbol)
 
        if current_price is not None and trade.entry_price:
            unrealized_pnl = (current_price - trade.entry_price) * trade.quantity
            unrealized_pnl_pct = (current_price - trade.entry_price) / trade.entry_price * 100
            total_unrealized_pnl += unrealized_pnl
        else:
            unrealized_pnl = None
            unrealized_pnl_pct = None
 
        open_positions.append({
            "trade": trade,
            "current_price": current_price,
            "unrealized_pnl": unrealized_pnl,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "is_gain": (unrealized_pnl or 0) >= 0,
        })
 
    closed_trades = [
        {"trade": trade, "is_gain": (trade.realized_pnl or 0) >= 0}
        for trade in closed_trades_qs
    ]
 
    total_realized_pnl = closed_trades_qs.aggregate(total=Sum("realized_pnl"))["total"] or 0
    closed_count = closed_trades_qs.count()
    winning_count = closed_trades_qs.filter(realized_pnl__gt=0).count()
    win_rate = (winning_count / closed_count * 100) if closed_count else None
 
    context = {
        "strategy": strategy,
        "entry_signal_desc": _describe_entry_signal(strategy),
        "universe_desc": _describe_universe(strategy),
        "sizing_desc": _describe_sizing(strategy),
        "risk_desc": _describe_risk_management(strategy),
        "open_positions": open_positions,
        "closed_trades": closed_trades,
        "total_realized_pnl": total_realized_pnl,
        "total_unrealized_pnl": total_unrealized_pnl,
        "closed_count": closed_count,
        "win_rate": win_rate,
    }
    return render(request, "strategy_detail.html", context)
 
 
@login_required
@require_POST
def strategy_toggle(request, strategy_id):
    # Scoped to request.user — this is what stops one user from
    # toggling another user's strategy by guessing/changing the ID
    # in the URL. get_object_or_404 with both filters means a
    # mismatched owner 404s exactly the same as a nonexistent ID,
    # so it doesn't even reveal that the strategy exists.
    strategy = get_object_or_404(Strategy, id=strategy_id, user=request.user)
    strategy.is_active = not strategy.is_active
    strategy.save(update_fields=["is_active"])
    return JsonResponse({"is_active": strategy.is_active})
 
 
def _parse_optional_float(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None
 
 
def _parse_optional_int(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
 
 
def _build_parameters_from_post(strategy_type, post):
    """
    Builds the strategy's `parameters` dict from the specific named
    form fields for whichever strategy_type was selected — the form
    only shows/submits the fields relevant to that type, so this
    only ever reads the ones that matter for it.
    """
    if strategy_type == "moving_average_crossover":
        return {
            "short_period": _parse_optional_int(post.get("ma_short_period")) or 10,
            "long_period": _parse_optional_int(post.get("ma_long_period")) or 30,
        }
 
    if strategy_type == "reddit_sentiment_threshold":
        params = {
            "min_mentions_24h": _parse_optional_int(post.get("rst_min_mentions_24h")) or 5,
            "min_positive_ratio_24h": _parse_optional_float(post.get("rst_min_positive_ratio_24h")) or 0.6,
        }
        acceleration = _parse_optional_float(post.get("rst_min_positive_acceleration_pct"))
        if acceleration is not None:
            params["min_positive_acceleration_pct"] = acceleration
        return params
 
    if strategy_type == "rsi_threshold":
        return {
            "rsi_period": _parse_optional_int(post.get("rsi_period")) or 14,
            "oversold_threshold": _parse_optional_float(post.get("rsi_oversold")) or 30,
            "overbought_threshold": _parse_optional_float(post.get("rsi_overbought")) or 70,
        }
 
    if strategy_type == "bollinger_reversion":
        return {
            "period": _parse_optional_int(post.get("bb_period")) or 20,
            "std_dev": _parse_optional_float(post.get("bb_std_dev")) or 2.0,
        }
 
    if strategy_type == "price_breakout":
        return {
            "breakout_period": _parse_optional_int(post.get("breakout_period")) or 20,
        }
 
    return {}
 
 
@login_required
def strategy_create(request):
    if request.method == "POST":
        strategy_type = request.POST.get("strategy_type")
        parameters = _build_parameters_from_post(strategy_type, request.POST)
        selected_sectors = request.POST.getlist("filter_sectors")
 
        Strategy.objects.create(
            user=request.user,
            name=request.POST.get("name", "").strip(),
            strategy_type=strategy_type,
            description=request.POST.get("description", "").strip(),
 
            symbols=request.POST.get("symbols", "").strip(),
            filter_sectors=",".join(selected_sectors),
            filter_min_price=_parse_optional_float(request.POST.get("filter_min_price")),
            filter_max_price=_parse_optional_float(request.POST.get("filter_max_price")),
            filter_min_day_change_pct=_parse_optional_float(request.POST.get("filter_min_day_change_pct")),
            filter_max_day_change_pct=_parse_optional_float(request.POST.get("filter_max_day_change_pct")),
            filter_min_reddit_mentions_24h=_parse_optional_int(request.POST.get("filter_min_reddit_mentions_24h")),
            filter_min_reddit_positive_ratio=_parse_optional_float(request.POST.get("filter_min_reddit_positive_ratio")),
            filter_min_reddit_positive_vs_negative_ratio=_parse_optional_float(request.POST.get("filter_min_reddit_positive_vs_negative_ratio")),
 
            parameters=parameters,
 
            position_sizing_method=request.POST.get("position_sizing_method", "fixed_shares"),
            position_sizing_value=_parse_optional_float(request.POST.get("position_sizing_value")) or 1,
 
            take_profit_pct=_parse_optional_float(request.POST.get("take_profit_pct")),
            stop_loss_pct=_parse_optional_float(request.POST.get("stop_loss_pct")),
            trailing_stop_pct=_parse_optional_float(request.POST.get("trailing_stop_pct")),
            max_hold_days=_parse_optional_int(request.POST.get("max_hold_days")),
            max_daily_entries_per_symbol=_parse_optional_int(request.POST.get("max_daily_entries_per_symbol")),
 
            is_active=False,
            is_paper=True,
        )
        return redirect("strategies")
 
    try:
        constituents = get_sp500_constituents()
        sectors = sorted({c["sector"] for c in constituents})
    except Exception:
        logger.exception("Failed to load sectors for strategy creation form")
        sectors = []
 
    return render(request, "strategy_create.html", {
        "strategy_type_choices": Strategy.STRATEGY_TYPE_CHOICES,
        "position_sizing_choices": POSITION_SIZING_CHOICES,
        "sectors": sectors,
    })

def get_sp500_constituents():
    """
    Returns a list of dicts (symbol, name, sector) for the current
    S&P 500 index membership, scraped from Wikipedia's maintained
    table. Cached for 24 hours since index membership changes rarely
    and there's no need to hit Wikipedia on every page load.
    """
    cached = cache.get("sp500_constituents")
    if cached:
        return cached
 
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
 
    # Wikipedia rejects requests without a browser-like User-Agent
    # (pandas.read_html(url) alone will 403). Fetch the HTML ourselves
    # first, then hand the text to pandas.
    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; AlbrightTrading/1.0)"},
        timeout=10,
    )
    response.raise_for_status()
 
    df = pd.read_html(io.StringIO(response.text))[0]
 
    constituents = [
        {
            # Alpaca's market data API expects the standard dotted
            # class-share notation (BRK.B), not BRK-B — leave symbols
            # exactly as Wikipedia lists them.
            "symbol": row["Symbol"],
            "name": row["Security"],
            "sector": row["GICS Sector"],
        }
        for _, row in df.iterrows()
    ]
 
    cache.set("sp500_constituents", constituents, 60 * 60 * 24)
    return constituents
 
 
def get_snapshot_rows(constituents):
    """
    Pulls a latest-price snapshot from Alpaca for each S&P 500 symbol
    and merges it with the constituent metadata. Cached briefly so
    repeated page loads don't hammer the API.
    """
    cached = cache.get("sp500_scanner_rows")
    if cached:
        return cached
 
    symbol_lookup = {c["symbol"]: c for c in constituents}
    symbols = list(symbol_lookup.keys())
 
    client = StockHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )
 
    rows = []
    chunk_size = 100  # defensive batching in case the list grows
 
    for i in range(0, len(symbols), chunk_size):
        chunk = symbols[i:i + chunk_size]
 
        try:
            snapshots = client.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=chunk)
            )
        except Exception:
            # One bad/delisted symbol shouldn't take down the whole
            # scanner — log it and keep going with the next chunk.
            logger.exception(
                "Snapshot request failed for chunk starting at index %d "
                "(symbols: %s)", i, ", ".join(chunk)
            )
            continue
 
        for symbol, snap in snapshots.items():
            if snap is None or snap.daily_bar is None:
                continue  # symbol didn't trade / no data returned
 
            last_price = float(snap.daily_bar.close)
            prev_close = (
                float(snap.previous_daily_bar.close)
                if snap.previous_daily_bar else last_price
            )
            change = last_price - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0
 
            meta = symbol_lookup[symbol]
            rows.append({
                "symbol": symbol,
                "name": meta["name"],
                "sector": meta["sector"],
                "price": last_price,
                "change": change,
                "change_pct": change_pct,
                "volume": int(snap.daily_bar.volume),
                "day_high": float(snap.daily_bar.high),
                "day_low": float(snap.daily_bar.low),
                "is_gain": change >= 0,
            })
 
    rows.sort(key=lambda r: r["symbol"])
 
    cache.set("sp500_scanner_rows", rows, 60)  # refresh at most once a minute
    return rows
 
 
def attach_reddit_sentiment(rows):
    """
    Merges today's Reddit mention counts (positive/neutral/negative)
    into each scanner row. Uses the same UTC date convention the
    run_reddit_sentiment command writes with, so "today" lines up
    with whatever the most recent scheduled run produced. Symbols
    with no Reddit activity today default to 0 across the board
    rather than being left out of the table.
    """
    today = dt.datetime.now(dt.timezone.utc).date()
 
    today_counts = RedditDailyMentionCount.objects.filter(date=today).values(
        "symbol", "positive_count", "neutral_count", "negative_count"
    )
    counts_by_symbol = {c["symbol"]: c for c in today_counts}
 
    for row in rows:
        counts = counts_by_symbol.get(row["symbol"])
        row["reddit_positive"] = counts["positive_count"] if counts else 0
        row["reddit_neutral"] = counts["neutral_count"] if counts else 0
        row["reddit_negative"] = counts["negative_count"] if counts else 0
 
    return rows
 
 
@login_required
def market_scanner(request):
    context = {"error": None, "rows": [], "as_of": None}
 
    try:
        constituents = get_sp500_constituents()
    except Exception:
        logger.exception("Failed to load S&P 500 constituent list")
        context["error"] = (
            "Couldn't load the S&P 500 constituent list. Please try again shortly."
        )
        return render(request, "market_scanner.html", context)
 
    try:
        rows = get_snapshot_rows(constituents)
    except Exception:
        logger.exception("Failed to load Alpaca market data for scanner")
        context["error"] = (
            "Couldn't reach Alpaca for market data. Please try again shortly."
        )
        return render(request, "market_scanner.html", context)
 
    rows = attach_reddit_sentiment(rows)
 
    context["rows"] = rows
    context["sectors"] = sorted({r["sector"] for r in rows})
    context["as_of"] = time.strftime("%I:%M:%S %p")
 
    return render(request, "market_scanner.html", context)

TIMEFRAME_OPTIONS = {
    "1D": {"label": "1 Day", "timeframe": TimeFrame.Day, "lookback_days": 380},
    "4H": {"label": "4 Hour", "timeframe": TimeFrame(4, TimeFrameUnit.Hour), "lookback_days": 60},
    "1H": {"label": "1 Hour", "timeframe": TimeFrame.Hour, "lookback_days": 30},
    "15M": {"label": "15 Min", "timeframe": TimeFrame(15, TimeFrameUnit.Minute), "lookback_days": 5},
}
 
 
def get_bars_for_symbol(symbol, tf_key):
    """
    Fetches historical bars for one symbol at the given timeframe key
    (must be a key in TIMEFRAME_OPTIONS) and returns a list of plain
    dicts ready for JSON serialization / the chart.
 
    Time is returned as a Unix timestamp (seconds, UTC) rather than a
    date string — Lightweight Charts requires numeric timestamps to
    render intraday (sub-daily) bars correctly; date strings only
    work for daily-or-coarser resolution.
    """
    option = TIMEFRAME_OPTIONS[tf_key]
 
    client = StockHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )
 
    bars_request = StockBarsRequest(
        symbol_or_symbols=[symbol],
        timeframe=option["timeframe"],
        start=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=option["lookback_days"]),
    )
    bars = client.get_stock_bars(bars_request).data.get(symbol, [])
 
    return [
        {
            "time": int(bar.timestamp.timestamp()),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": int(bar.volume),
        }
        for bar in bars
    ]
 
 
@login_required
def stock_bars_api(request, symbol):
    """
    JSON endpoint the chart calls when the user switches timeframes.
    GET /stock/<symbol>/bars/?tf=1H  (tf defaults to 1D)
    """
    symbol = symbol.upper()
    tf_key = request.GET.get("tf", "1D").upper()
 
    if tf_key not in TIMEFRAME_OPTIONS:
        return JsonResponse({"error": "Invalid timeframe."}, status=400)
 
    try:
        bars = get_bars_for_symbol(symbol, tf_key)
    except Exception:
        logger.exception("Failed to fetch %s bars for %s", tf_key, symbol)
        return JsonResponse({"error": "Couldn't fetch chart data."}, status=502)
 
    return JsonResponse({"bars": bars})
 
 
# ============================================================
# ADD this function anywhere in views.py:
# ============================================================
 
@login_required
def stock_detail(request, symbol):
    symbol = symbol.upper()
    context = {"error": None, "symbol": symbol}
 
    try:
        constituents = get_sp500_constituents()
    except Exception:
        logger.exception("Failed to load S&P 500 constituent list for stock detail")
        context["error"] = "Couldn't load company data. Please try again shortly."
        return render(request, "stock_detail.html", context)
 
    meta = next((c for c in constituents if c["symbol"] == symbol), None)
    if meta is None:
        context["error"] = f"{symbol} isn't in the S&P 500 constituent list."
        return render(request, "stock_detail.html", context)
 
    context["name"] = meta["name"]
    context["sector"] = meta["sector"]
 
    client = StockHistoricalDataClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
    )
 
    # ---- Latest snapshot: price, change, day range, bid/ask ----
    try:
        snapshot = client.get_stock_snapshot(
            StockSnapshotRequest(symbol_or_symbols=[symbol])
        ).get(symbol)
    except Exception:
        logger.exception("Failed to fetch snapshot for %s", symbol)
        context["error"] = "Couldn't reach Alpaca for this stock's data. Please try again shortly."
        return render(request, "stock_detail.html", context)
 
    if snapshot is None or snapshot.daily_bar is None:
        context["error"] = f"No market data available for {symbol} right now."
        return render(request, "stock_detail.html", context)
 
    last_price = float(snapshot.daily_bar.close)
    prev_close = (
        float(snapshot.previous_daily_bar.close)
        if snapshot.previous_daily_bar else last_price
    )
    change = last_price - prev_close
    change_pct = (change / prev_close * 100) if prev_close else 0
 
    context.update({
        "price": last_price,
        "change": change,
        "change_pct": change_pct,
        "is_gain": change >= 0,
        "day_high": float(snapshot.daily_bar.high),
        "day_low": float(snapshot.daily_bar.low),
        "volume": int(snapshot.daily_bar.volume),
        "bid_price": float(snapshot.latest_quote.bid_price) if snapshot.latest_quote else None,
        "ask_price": float(snapshot.latest_quote.ask_price) if snapshot.latest_quote else None,
    })
 
    # ---- Historical daily bars: initial chart paint (1D default) + 52-week range ----
    try:
        bars_data = get_bars_for_symbol(symbol, "1D")
    except Exception:
        logger.exception("Failed to fetch historical bars for %s", symbol)
        bars_data = []
 
    context["chart_data_json"] = json.dumps(bars_data)
    context["timeframe_options"] = [
        {"key": key, "label": opt["label"]} for key, opt in TIMEFRAME_OPTIONS.items()
    ]
    context["default_timeframe"] = "1D"
 
    if bars_data:
        context["week52_high"] = max(b["high"] for b in bars_data)
        context["week52_low"] = min(b["low"] for b in bars_data)
        context["avg_volume"] = int(sum(b["volume"] for b in bars_data) / len(bars_data))
    else:
        context["week52_high"] = None
        context["week52_low"] = None
        context["avg_volume"] = None
 
    # ---- Reddit sentiment summary (24h / 7d / 30d + trend) ----
    context["reddit_summary"] = RedditSentimentSummary.objects.filter(
        symbol=symbol
    ).first()
 
    return render(request, "stock_detail.html", context)

def user_login(request):

    login_form = LoginForm()

    if request.method == 'POST':

        login_form = LoginForm(request, data=request.POST)

        if login_form.is_valid():

            username = request.POST.get('username')
            password = request.POST.get('password')

            user = authenticate(request, username=username, password=password)

            if user is not None:

                auth.login(request, user)

                return redirect('/')
    context = {'login_form':login_form}

    return render(request,'user_login.html', context)

@login_required
def user_logout(request):
    logout(request)
    return HttpResponseRedirect(reverse('user_login'))

def registration(request):

    registered = False

    if request.method == "POST":
        user_form = UserForm(data=request.POST)

        if user_form.is_valid():

            user = user_form.save()
            user.set_password(user.password)
            user.save()

            registered = True

            return redirect('/registrationsuccess')

        else:
            print(user_form.errors)

    else:
        user_form = UserForm()

    context = {'user_form': user_form,}

    return render(request, 'registration.html', context=context)

def registrationsuccess(request):
    return render(request,'registrationsuccess.html')