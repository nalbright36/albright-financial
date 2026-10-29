"""Grok Bot monitor - READ-ONLY views of the Alpaca paper account an
external bot (Grok) trades options in.

Nothing in this module can place, modify or cancel an order: the page
only ever talks to Alpaca through ReadOnlyAlpaca, which exposes exactly
five read calls (account, positions, orders, portfolio history, fill
activities) and nothing else - the underlying TradingClient isn't
reachable from the outside. tests/test_grok_bot.py checks the view never
touches an order-submission or cancel method.

Several bot strategies trade in the one account, told apart by their
client_order_id prefix (BotStrategy rows); strategy_attribution.py says
which strategy each order, fill and dollar of P&L belongs to.

Pure helpers (no I/O, easy to test):
  - match_fills(): fills -> closed trades, when each still-open position
    was opened, and the P&L each fill realized (FIFO, via
    strategy_attribution.FifoLedger)
  - parse_occ(): "AAPL250117C00190000" -> underlying, expiration, call/put, strike
  - closed_trades(): fills -> realized P&L per closed position (FIFO lots,
    long or short; a position "closes" when it returns to flat)
  - trade_stats(): win rate, average win/loss, count, total
  - order_ids_to_client_ids(): every order and leg id -> its (parent's) client order ID
"""
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone as dt_timezone
from decimal import Decimal, InvalidOperation

from django.core.cache import cache
from django.utils import timezone

log = logging.getLogger(__name__)

CACHE_SECONDS = 60
RECENT_ORDERS = 50
ORDER_HISTORY_LIMIT = 500        # Alpaca's maximum per request
MAX_ORDER_PAGES = 20             # up to 10,000 orders back to the period's start
# Every order back to the account's start is fetched (and cached) whatever
# the period, so each strategy's FIFO has its opening fills - an open
# attributed to no one would make its close look like a new short.
FILL_PAGE_SIZE = 100
MAX_FILL_PAGES = 50              # up to 5,000 fills - the whole history for most paper bots
OPTION_MULTIPLIER = 100

# Chart toggles -> portfolio history request. "all" starts at the account's creation.
PERIODS = {
    "1W": {"label": "1W", "period": "1W", "timeframe": "1H", "days": 7},
    "1M": {"label": "1M", "period": "1M", "timeframe": "1D", "days": 30},
    "3M": {"label": "3M", "period": "3M", "timeframe": "1D", "days": 91},
    "1Y": {"label": "1Y", "period": "1A", "timeframe": "1D", "days": 365},
    "all": {"label": "All", "period": None, "timeframe": "1D", "days": None},
}
DEFAULT_PERIOD = "1M"

OCC_RE = re.compile(r"^([A-Z][A-Z0-9.]{0,5})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


# ---------------------------------------------------------------------------
# Small conversions
# ---------------------------------------------------------------------------

def _dec(value, default=None):
    if value in (None, ""):
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


def _get(obj, name, default=None):
    """Attribute or dict key - Alpaca models and raw JSON dicts alike."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _enum(value):
    """'OrderSide.BUY' / OrderSide.BUY / 'buy' -> 'buy'."""
    value = getattr(value, "value", value)
    return str(value or "").split(".")[-1].lower()


def _dt(value):
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt_timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


# ---------------------------------------------------------------------------
# OCC option symbols
# ---------------------------------------------------------------------------

def parse_occ(symbol):
    """OCC option symbol -> dict, or None for anything that isn't one (stocks).

    Format: root (1-6 chars) + YYMMDD + C/P + strike x 1000 in 8 digits,
    e.g. "AAPL250117C00190000" = AAPL, 2025-01-17, call, $190. Alpaca
    sends it without the OCC padding spaces; padded symbols work too."""
    match = OCC_RE.match(re.sub(r"\s+", "", str(symbol or "").upper()))
    if not match:
        return None
    root, yy, mm, dd, kind, strike = match.groups()
    try:
        expiration = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return {
        "underlying": root,
        "expiration": expiration,
        "type": "call" if kind == "C" else "put",
        "strike": Decimal(strike) / 1000,
    }


def describe_contract(symbol):
    """'AAPL 2025-01-17 $190 call' (strike without trailing zeros), or the symbol itself for a stock."""
    occ = parse_occ(symbol)
    if not occ:
        return symbol
    strike = occ["strike"].normalize()
    strike_text = f"{strike:f}" if strike != strike.to_integral() else f"{int(strike)}"
    return f"{occ['underlying']} {occ['expiration']:%b %d, %Y} ${strike_text} {occ['type']}"


def multiplier(symbol):
    return OPTION_MULTIPLIER if parse_occ(symbol) else 1


def days_to_expiration(symbol, today=None):
    occ = parse_occ(symbol)
    if not occ:
        return None
    return (occ["expiration"] - (today or timezone.localdate())).days


# ---------------------------------------------------------------------------
# Client order IDs
# ---------------------------------------------------------------------------

def matches_prefix(client_order_id, prefix):
    """Case-insensitive startswith ("ENG" matches "eng-test-open-...")."""
    return not prefix or str(client_order_id or "").casefold().startswith(prefix.casefold())


def order_ids_to_client_ids(orders):
    """{order id: client order id} for every order and every leg of a
    multi-leg order. A leg takes its parent's client order ID - the bot
    tags the order it submits, and Alpaca reports leg fills by leg id."""
    mapping = {}
    for order in orders:
        client_id = _get(order, "client_order_id") or ""
        mapping[str(_get(order, "id"))] = client_id
        for leg in _get(order, "legs") or []:
            mapping[str(_get(leg, "id"))] = client_id
    return mapping


# ---------------------------------------------------------------------------
# Realized P&L from fills
# ---------------------------------------------------------------------------

@dataclass
class ClosedTrade:
    symbol: str
    opened_at: datetime
    closed_at: datetime
    direction: str          # "long" or "short"
    quantity: Decimal       # largest position size held
    pnl: Decimal = Decimal("0")
    expired: bool = False   # closed at $0 by expiring (no closing fill)
    strategy: str = ""      # the strategy key it belongs to (set by the page)

    @property
    def description(self):
        return describe_contract(self.symbol)

    @property
    def is_win(self):
        return self.pnl > 0


def normalize_fill(activity):
    """A FILL activity (raw dict or model) -> {id, order_id, symbol, side, qty, price, time, fee}.
    Alpaca's FILL activities usually carry no fee; one is used when present ("fee" or "commission")."""
    side = _enum(_get(activity, "side"))
    return {
        "id": str(_get(activity, "id") or ""),
        "order_id": str(_get(activity, "order_id") or ""),
        "symbol": str(_get(activity, "symbol") or ""),
        "side": "sell" if side.startswith("sell") else "buy",
        "qty": abs(_dec(_get(activity, "qty"), Decimal("0"))),
        "price": _dec(_get(activity, "price"), Decimal("0")),
        "time": _dt(_get(activity, "transaction_time")),
        "fee": abs(_dec(_get(activity, "fee"), None) or _dec(_get(activity, "commission"), Decimal("0"))),
    }


@dataclass
class Realization:
    """P&L one fill realized by reducing a position, and the entry cost of what it closed."""
    time: datetime
    symbol: str
    pnl: Decimal
    cost_basis: Decimal


def closed_trades(fills):
    """The closed trades from match_fills()."""
    return match_fills(fills)[0]


def match_fills(fills):
    """Returns (closed trades, {symbol: when its still-open position was
    opened}, [Realization per reducing fill]).

    Fills are matched first-in-first-out per symbol, longs and shorts alike
    (an options bot often sells to open) - strategy_attribution.FifoLedger.
    Every reduction of a position realizes (exit - entry) x qty x
    multiplier, reversed for a short. A "closed trade" is one round trip
    from flat back to flat; its P&L is everything realized along the way
    (scale-ins and partial exits included). A fill that flips the position
    closes the trade and opens a new one with the remainder. Positions still
    open aren't closed trades."""
    from .strategy_attribution import ledger_from
    ledger = ledger_from(fills)
    open_since = {symbol: trade.opened_at for symbol, trade in ledger.open_trades.items()}
    return ledger.trades, open_since, ledger.realizations


def trade_stats(trades):
    """Win rate = winning trades / all closed trades (a $0 trade counts as
    a trade but not a win). Average win / loss are over winners / losers."""
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    count = len(trades)
    return {
        "count": count,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (Decimal(len(wins)) / count * 100).quantize(Decimal("0.1")) if count else None,
        "avg_win": (sum(wins) / len(wins)).quantize(Decimal("0.01")) if wins else None,
        "avg_loss": (sum(losses) / len(losses)).quantize(Decimal("0.01")) if losses else None,
        "total": sum((t.pnl for t in trades), Decimal("0")).quantize(Decimal("0.01")),
    }


def period_start(period, now=None):
    days = PERIODS[period]["days"]
    return (now or timezone.now()) - timedelta(days=days) if days else None


# ---------------------------------------------------------------------------
# Alpaca access - read-only, paper only, cached 60 seconds
# ---------------------------------------------------------------------------

class ReadOnlyAlpaca:
    """The only way this page talks to Alpaca. It holds the TradingClient
    privately and exposes five reads - there is no method here (or
    reachable through it) that submits, replaces, cancels or closes
    anything."""
    FILL_ACTIVITIES_PATH = "/account/activities/FILL"

    def __init__(self, api_key, secret_key):
        from alpaca.trading.client import TradingClient
        self.__client = TradingClient(api_key=api_key, secret_key=secret_key, paper=True)  # paper endpoint only
        self.__keys = {"api_key": api_key, "secret_key": secret_key}   # for market-data reads (price_at)

    def account(self):
        return self.__client.get_account()

    def positions(self):
        return self.__client.get_all_positions()

    def orders(self, limit, after=None, until=None):
        """One page of orders, newest first (status all, nested legs)."""
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest
        params = {"status": QueryOrderStatus.ALL, "limit": limit, "nested": True}
        if after:
            params["after"] = after
        if until:
            params["until"] = until
        return self.__client.get_orders(GetOrdersRequest(**params))

    def portfolio_history(self, period, timeframe, start=None):
        from alpaca.trading.requests import GetPortfolioHistoryRequest
        params = {"timeframe": timeframe}
        if period:
            params["period"] = period
        if start:
            params["start"] = start
        return self.__client.get_portfolio_history(GetPortfolioHistoryRequest(**params))

    def price_at(self, symbol, when):
        """The close of the last daily bar before `when`'s date - the price
        at that period's start - from Alpaca market data, or None if there
        isn't one. Options and stocks alike (a read; nothing is traded)."""
        from alpaca.data.timeframe import TimeFrame
        request = {"symbol_or_symbols": symbol, "timeframe": TimeFrame.Day,
                   "start": when - timedelta(days=10), "end": when}
        if parse_occ(symbol):
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.requests import OptionBarsRequest
            bars = OptionHistoricalDataClient(**self.__keys).get_option_bars(OptionBarsRequest(**request))
        else:
            from alpaca.data.historical.stock import StockHistoricalDataClient
            from alpaca.data.requests import StockBarsRequest
            bars = StockHistoricalDataClient(**self.__keys).get_stock_bars(StockBarsRequest(**request))
        series = (getattr(bars, "data", None) or (bars if isinstance(bars, dict) else {})).get(symbol) or []
        closes = [_get(b, "close") for b in series
                  if _dt(_get(b, "timestamp")) and timezone.localtime(_dt(_get(b, "timestamp"))).date()
                  < timezone.localtime(when).date()]
        return _dec(closes[-1]) if closes else None

    def fill_activities(self, page_token=None):
        """One page of FILL activities, newest first (a plain GET -
        alpaca-py 0.44 has no wrapper for account activities)."""
        params = {"direction": "desc", "page_size": FILL_PAGE_SIZE}
        if page_token:
            params["page_token"] = page_token
        return self.__client.get(self.FILL_ACTIVITIES_PATH, params) or []


class GrokBotError(Exception):
    """Something the page shows as a message instead of data."""


def _cache_key(user_id, key):
    return f"grok_bot:{user_id}:{key}"


def clear_cache(user_id):
    """Drops every cached Alpaca response for this user."""
    keys = ["account", "positions", "fills", "orders:all"] + [f"history:{p}" for p in PERIODS]
    cache.delete_many([_cache_key(user_id, k) for k in keys])


def _cached(user_id, key, fetch):
    cache_key = _cache_key(user_id, key)
    value = cache.get(cache_key)
    if value is None:
        value = fetch()
        cache.set(cache_key, value, CACHE_SECONDS)
    return value


def _account_data(api):
    a = api.account()
    equity, last_equity = _dec(a.equity, Decimal("0")), _dec(a.last_equity, Decimal("0"))
    change = equity - last_equity
    return {
        "equity": equity, "cash": _dec(a.cash, Decimal("0")), "buying_power": _dec(a.buying_power, Decimal("0")),
        "last_equity": last_equity, "day_pl": change,
        "day_pl_pct": (change / last_equity * 100) if last_equity else None,
        "created_at": _dt(_get(a, "created_at")),
    }


def _positions_data(api, today=None):
    rows = []
    for p in api.positions():
        symbol = p.symbol
        occ = parse_occ(symbol)
        rows.append({
            "symbol": symbol,
            "description": describe_contract(symbol) if occ else "",
            "asset": "option" if occ else "stock",
            "qty": _dec(p.qty, Decimal("0")),
            "side": _enum(_get(p, "side")),
            "avg_entry": _dec(p.avg_entry_price),
            "current_price": _dec(p.current_price),
            "market_value": _dec(p.market_value),
            "unrealized_pl": _dec(p.unrealized_pl, Decimal("0")),
            "unrealized_pl_pct": _dec(p.unrealized_plpc, Decimal("0")) * 100,
            "unrealized_intraday_pl": _dec(_get(p, "unrealized_intraday_pl")),
            "lastday_price": _dec(_get(p, "lastday_price")),
            "cost_basis": _dec(_get(p, "cost_basis")),
            "dte": days_to_expiration(symbol, today),
        })
    return sorted(rows, key=lambda r: (r["dte"] is None, r["dte"] or 0, r["symbol"]))


def _order_row(o):
    legs = _get(o, "legs") or []
    symbol = _get(o, "symbol")
    is_multi_leg = _enum(_get(o, "order_class")) == "mleg" or bool(legs and not symbol)
    leg_rows = [{"symbol": _get(leg, "symbol"), "description": describe_contract(_get(leg, "symbol")),
                 "side": _enum(_get(leg, "side")), "ratio": _get(leg, "ratio_qty"),
                 "filled": (_dec(_get(leg, "filled_qty"), Decimal("0")) > 0
                            or _enum(_get(leg, "status")) in ("filled", "partially_filled"))} for leg in legs]
    status = _enum(_get(o, "status"))
    return {
        "time": _dt(_get(o, "submitted_at")) or _dt(_get(o, "created_at")),
        "symbols": [leg["symbol"] for leg in leg_rows] if is_multi_leg else [symbol],
        "description": "" if is_multi_leg or not parse_occ(symbol) else describe_contract(symbol),
        "legs": leg_rows if is_multi_leg else [],
        "is_multi_leg": is_multi_leg,
        "side": "multi-leg" if is_multi_leg else _enum(_get(o, "side")),
        "qty": _dec(_get(o, "qty")),
        "type": _enum(_get(o, "order_type") or _get(o, "type")),
        "limit_price": _dec(_get(o, "limit_price")),
        "status": status,
        "filled": _dec(_get(o, "filled_qty"), Decimal("0")) > 0 or status in ("filled", "partially_filled"),
        "filled_avg_price": _dec(_get(o, "filled_avg_price")),
        "client_order_id": _get(o, "client_order_id") or "",
    }


def _orders_data(api, after=None):
    """Every order back to `after` (None: all), 500 per page, newest
    first - each next page ends ("until") at the oldest order seen so far.
    Used both for Recent orders and to map fills (by order or leg id) to
    their client order IDs."""
    orders, seen, until, truncated = [], set(), None, False
    for _page in range(MAX_ORDER_PAGES):
        batch = api.orders(ORDER_HISTORY_LIMIT, after=after, until=until)
        new = [o for o in batch if str(_get(o, "id")) not in seen]
        orders += new
        seen.update(str(_get(o, "id")) for o in new)
        if len(batch) < ORDER_HISTORY_LIMIT or not new:
            break
        until = min(_dt(_get(o, "submitted_at")) or _dt(_get(o, "created_at")) for o in new)
    else:
        truncated = True
    return {"rows": [_order_row(o) for o in orders], "client_ids": order_ids_to_client_ids(orders),
            "truncated": truncated}


def _fills_data(api):
    fills, token, truncated = [], None, False
    for _page in range(MAX_FILL_PAGES):
        batch = api.fill_activities(token)
        fills += [normalize_fill(a) for a in batch]
        if len(batch) < FILL_PAGE_SIZE:
            break
        token = str(_get(batch[-1], "id"))
    else:
        truncated = True
    return {"fills": fills, "truncated": truncated}


def _history_data(api, period, account_created):
    spec = PERIODS[period]
    h = api.portfolio_history(spec["period"], spec["timeframe"], start=account_created if period == "all" else None)
    points = [{"time": int(ts), "value": float(eq)} for ts, eq in zip(h.timestamp or [], h.equity or [])
              if eq not in (None, 0)]
    pl = [v for v in (h.profit_loss or []) if v is not None]
    pl_pct = [v for v in (h.profit_loss_pct or []) if v is not None]
    if pl:
        total = _dec(pl[-1])
    elif points:
        total = Decimal(str(points[-1]["value"])) - _dec(h.base_value, Decimal("0"))
    else:
        total = None
    return {"points": points, "total_pl": total, "total_pl_pct": _dec(pl_pct[-1]) * 100 if pl_pct else None}


def build_dashboard(credentials, user_id, strategies=(), period=DEFAULT_PERIOD, today=None, now=None):
    """Everything the page shows, with every order, fill, trade and open
    position attributed to a strategy (strategy_attribution). `strategies`:
    StrategyRefs in display order. Each Alpaca section is fetched (and
    cached) separately, so one failing call leaves the others on the page;
    its message lands in data["errors"]. Raises GrokBotError when nothing can
    be fetched at all (live account, unreadable credentials)."""
    from . import strategy_attribution as sa
    if not credentials.is_paper:
        raise GrokBotError("Your saved Alpaca account is a live account. The Grok Bot page only reads paper "
                           "accounts - connect your paper account's keys to use it.")
    try:
        api = ReadOnlyAlpaca(credentials.get_api_key(), credentials.get_secret_key())
    except Exception as exc:  # noqa: BLE001 - e.g. the encryption key changed
        log.warning("Grok Bot: couldn't load Alpaca credentials: %s", exc)
        raise GrokBotError("Couldn't read your saved Alpaca credentials - try reconnecting your account.") from exc

    period = period if period in PERIODS else DEFAULT_PERIOD
    today = today or timezone.localdate()
    refs = list(strategies)
    names = {ref.key: ref for ref in refs + [sa.UNATTRIBUTED_REF]}
    data = {"errors": {}, "period": period, "strategies": refs, "names": names}

    def section(name, key, fetch):
        try:
            return _cached(user_id, key, fetch)
        except Exception as exc:  # noqa: BLE001 - API/network errors become a message, never a broken page
            log.warning("Grok Bot: %s failed: %s", name, exc)
            data["errors"][name] = f"Couldn't load {name} from Alpaca right now."
            return None

    data["account"] = section("account", "account", lambda: _account_data(api))
    created = data["account"]["created_at"] if data["account"] else None
    data["history"] = section("portfolio history", f"history:{period}", lambda: _history_data(api, period, created))
    data["positions"] = section("positions", "positions", lambda: _positions_data(api, today))
    start = period_start(period, now)
    orders = section("orders", "orders:all", lambda: _orders_data(api))
    fills = section("fills", "fills", lambda: _fills_data(api))

    data["orders"] = data["orders_truncated"] = data["orders_scanned"] = None
    if orders is not None:
        per_bucket = {}
        for row in orders["rows"]:                       # newest first
            row["strategy"] = sa.match_strategy(row["client_order_id"], refs)
            per_bucket.setdefault(row["strategy"], []).append(row)
        # The most recent RECENT_ORDERS of each strategy, so filtering by one still shows its own.
        data["orders"] = sorted((r for rows in per_bucket.values() for r in rows[:RECENT_ORDERS]),
                                key=lambda r: r["time"] or datetime.min.replace(tzinfo=dt_timezone.utc), reverse=True)
        data["orders_truncated"] = orders["truncated"]
        data["orders_scanned"] = len(orders["rows"])
        data["unmatched_strategies"] = [ref for ref in refs if ref.key not in per_bucket]

    data["report"] = data["trades"] = data["fills_truncated"] = None
    if fills is not None and orders is None:
        data["errors"]["strategies"] = "Strategy results need the order list to tell whose fills are whose."
    elif fills is not None:
        buckets = sa.split_fills(fills["fills"], sa.order_strategies(orders["client_ids"], refs))
        account_positions = {p["symbol"]: p for p in data["positions"] or []}
        lookup = lambda symbol: (lambda v: None if v in (None, "none") else v)(  # noqa: E731
            _price_at_start(api, user_id, symbol, start))
        rows = [sa.bucket_report(ref, buckets.get(ref.key, []), account_positions, start, today, lookup)
                for ref in refs + [sa.UNATTRIBUTED_REF]]
        sa.mark_shared(rows)
        data["report"] = {"rows": rows[:-1], "unattributed": rows[-1], "total": sa.total_report(rows),
                          "by_key": {row["key"]: row for row in rows}}
        trades = []
        for row in rows:
            for trade in row["trades"]:
                trade.strategy = row["key"]
                trades.append(trade)
        data["trades"] = sorted(trades, key=lambda t: t.closed_at, reverse=True)
        data["fills_truncated"] = fills["truncated"]
        holders = sa.position_holders(rows)
        for position in data["positions"] or []:
            held = holders.get(position["symbol"], [])
            position["holders"] = [{"key": row["key"], "name": row["name"], "color": row["color"], "qty": qty}
                                   for row, qty in held]
            position["strategies"] = " ".join(h["key"] for h in position["holders"]) or sa.UNATTRIBUTED
            position["shared"] = len(held) > 1
            # What the account holds that no bucket's fills explain (e.g. a fill older than the history read).
            position["unaccounted"] = abs(position["qty"]) * (-1 if position["side"] == "short" else 1) \
                - sum((qty for _row, qty in held), Decimal("0"))

    data["tiles"] = _account_tiles(data)
    return data


def _price_at_start(api, user_id, symbol, start):
    """Cached; None when market data has no bar (or the request fails)."""
    try:
        return _cached(user_id, f"price:{symbol}:{start.date()}", lambda: api.price_at(symbol, start) or "none") \
            if start else None
    except Exception as exc:  # noqa: BLE001 - no price just means "count the full unrealized P&L"
        log.info("Grok Bot: no period-start price for %s: %s", symbol, exc)
        return None


def _account_tiles(data):
    """The whole account's Today's P&L (equity vs last equity) and Total P&L (the portfolio history's)."""
    account, history = data["account"], data["history"]
    return {
        "scope": "whole account",
        "today": {"value": account["day_pl"], "pct": account["day_pl_pct"]} if account else None,
        "total": ({"value": history["total_pl"], "pct": history["total_pl_pct"]}
                  if history and history["total_pl"] is not None else None),
    }
