from django.shortcuts import render
from albright_trading_app.forms import UserForm, LoginForm, InvestorProfileForm
from django.urls import reverse
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.http import HttpResponseRedirect,HttpResponse
from django.contrib.auth import authenticate,login,logout
from django.contrib.auth.models import auth
from django.shortcuts import redirect
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

def home(request):
    context = {
               }
    return render(request,'home.html',context=context)

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

@login_required
def strategies(request):
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

    context = {"strategy_rows": strategy_rows}
    return render(request, 'strategies.html', context=context)

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
 
 
@login_required
def strategy_create(request):
    if request.method == "POST":
        params_raw = request.POST.get("parameters", "").strip() or "{}"
        try:
            parameters = json.loads(params_raw)
        except ValueError:
            parameters = {}
 
        selected_sectors = request.POST.getlist("filter_sectors")
 
        Strategy.objects.create(
            user=request.user,
            name=request.POST.get("name", "").strip(),
            strategy_type=request.POST.get("strategy_type"),
            description=request.POST.get("description", "").strip(),
 
            symbols=request.POST.get("symbols", "").strip(),
            filter_sectors=",".join(selected_sectors),
            filter_min_price=_parse_optional_float(request.POST.get("filter_min_price")),
            filter_max_price=_parse_optional_float(request.POST.get("filter_max_price")),
            filter_min_day_change_pct=_parse_optional_float(request.POST.get("filter_min_day_change_pct")),
            filter_max_day_change_pct=_parse_optional_float(request.POST.get("filter_max_day_change_pct")),
            filter_min_reddit_mentions_24h=_parse_optional_int(request.POST.get("filter_min_reddit_mentions_24h")),
            filter_min_reddit_positive_ratio=_parse_optional_float(request.POST.get("filter_min_reddit_positive_ratio")),
 
            parameters=parameters,
 
            position_sizing_method=request.POST.get("position_sizing_method", "fixed_shares"),
            position_sizing_value=_parse_optional_float(request.POST.get("position_sizing_value")) or 1,
 
            take_profit_pct=_parse_optional_float(request.POST.get("take_profit_pct")),
            stop_loss_pct=_parse_optional_float(request.POST.get("stop_loss_pct")),
            max_hold_days=_parse_optional_int(request.POST.get("max_hold_days")),
 
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
 
# ADD to your views.py imports:
#   from .models import POSITION_SIZING_CHOICES
 
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