"""Which bot strategy each order, fill and dollar of P&L belongs to - for
the Grok Bot page, where several strategies trade in ONE Alpaca paper
account and are told apart by their client_order_id prefix.

Everything here is a pure function (no database, no Alpaca), so it's easy
to test:

  - match_strategy(): a client_order_id -> the strategy whose prefix it
    starts with (any capitalization; the longest matching prefix wins), or
    UNATTRIBUTED ("Unattributed (manual/other)").
  - ambiguities(): warnings for prefixes two strategies share, or where one
    starts with another.
  - order_strategies() / split_fills(): every order AND every leg of a
    multi-leg order takes its parent's client_order_id (legs carry Alpaca's
    own ids), and each fill goes to its order's strategy. A fill whose order
    isn't in the fetched history can't be attributed: Unattributed.
  - FifoLedger: one strategy's own fills matched first-in-first-out per
    contract (options x100, fees subtracted when present). Two strategies
    holding the same contract each keep their own quantity and cost basis.
    An option still open after its expiration (and no longer in the
    account) is closed at $0 - Alpaca records expiry as an activity, not a
    fill.
  - bucket_report() / total_report(): the comparison table's numbers.
  - daily_history(): realized P&L to date per day, for the snapshot backfill.
  - cumulative_series(): one strategy's cumulative P&L per day, for the chart.

Read-only: nothing here (or anything it's given) places, changes or
cancels orders.
"""
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from decimal import Decimal

from django.utils import timezone

from .grok_bot import (ClosedTrade, Realization, days_to_expiration, describe_contract, multiplier, parse_occ,
                       trade_stats)

UNATTRIBUTED = "unattributed"
UNATTRIBUTED_NAME = "Unattributed (manual/other)"
UNATTRIBUTED_COLOR = "#94A3B8"
PREFIX_RE = re.compile(r"^[A-Za-z0-9._:-]+$")
ZERO = Decimal("0")
CENT = Decimal("0.01")


@dataclass(frozen=True)
class StrategyRef:
    """What attribution needs to know about a strategy (a BotStrategy row, or the Unattributed bucket)."""
    key: str
    name: str
    prefixes: tuple = ()
    color: str = UNATTRIBUTED_COLOR
    status: str = "active"
    capital: Decimal = None
    pk: int = None

    @property
    def is_unattributed(self):
        return self.key == UNATTRIBUTED


UNATTRIBUTED_REF = StrategyRef(UNATTRIBUTED, UNATTRIBUTED_NAME)


def refs_from(strategies):
    """BotStrategy rows (in display order) -> StrategyRefs keyed by their pk."""
    return [StrategyRef(key=str(s.pk), name=s.name, prefixes=tuple(s.prefixes or ()), color=s.color, status=s.status,
                        capital=s.allocated_capital, pk=s.pk) for s in strategies]


# ---------------------------------------------------------------------------
# Prefixes
# ---------------------------------------------------------------------------

def parse_prefixes(text):
    """"eng, ENG-v2  intra" -> ["eng", "ENG-v2", "intra"] (commas or spaces; duplicates in any case dropped)."""
    prefixes, seen = [], set()
    for part in re.split(r"[,\s]+", text or ""):
        if part and part.casefold() not in seen:
            seen.add(part.casefold())
            prefixes.append(part)
    return prefixes


def match_strategy(client_order_id, strategies):
    """The key of the strategy whose prefix client_order_id starts with (case-insensitive). When several
    match, the longest prefix wins (then the earlier strategy). UNATTRIBUTED when none do."""
    cid = str(client_order_id or "").casefold()
    best, best_length = UNATTRIBUTED, 0
    for strategy in strategies:
        for prefix in strategy.prefixes:
            if prefix and cid.startswith(prefix.casefold()) and len(prefix) > best_length:
                best, best_length = strategy.key, len(prefix)
    return best


def ambiguities(strategies):
    """Warnings for prefixes that make attribution ambiguous between two strategies."""
    owned = [(prefix, strategy) for strategy in strategies for prefix in strategy.prefixes]
    warnings = []
    for i, (prefix, strategy) in enumerate(owned):
        for other_prefix, other in owned[i + 1:]:
            if other.key == strategy.key:
                continue
            a, b = prefix.casefold(), other_prefix.casefold()
            if a == b:
                warnings.append(f'"{strategy.name}" and "{other.name}" both use the prefix "{prefix}" - their orders '
                                f'can\'t be told apart (they all go to "{strategy.name}").')
            elif a.startswith(b) or b.startswith(a):
                longer, longer_owner = (prefix, strategy) if len(a) > len(b) else (other_prefix, other)
                shorter, shorter_owner = (other_prefix, other) if longer_owner is strategy else (prefix, strategy)
                warnings.append(f'"{longer}" ({longer_owner.name}) starts with "{shorter}" ({shorter_owner.name}) - an '
                                f'order starting with "{longer}" matches both; it goes to {longer_owner.name}, the '
                                f'longer prefix.')
    return warnings


def order_strategies(client_ids, strategies):
    """{order or leg id: strategy key}, from grok_bot.order_ids_to_client_ids() (legs already carry their
    parent's client_order_id)."""
    return {order_id: match_strategy(client_id, strategies) for order_id, client_id in client_ids.items()}


def split_fills(fills, id_to_key):
    """{strategy key: [its fills]}; a fill whose order isn't known goes to UNATTRIBUTED."""
    buckets = defaultdict(list)
    for fill in fills:
        buckets[id_to_key.get(str(fill["order_id"]), UNATTRIBUTED)].append(fill)
    return buckets


# ---------------------------------------------------------------------------
# FIFO per strategy
# ---------------------------------------------------------------------------

@dataclass
class Lot:
    qty: Decimal        # signed: + long, - short
    price: Decimal
    time: datetime


_EARLIEST = datetime.min.replace(tzinfo=timezone.get_fixed_timezone(0))


def _local_date(value):
    return timezone.localtime(value).date() if value else None


class FifoLedger:
    """One strategy's fills, matched first-in-first-out per contract, longs and shorts alike. Every
    reduction of a position realizes (exit - entry) x qty x multiplier (reversed for a short); a fill's
    fee (if Alpaca sent one) is subtracted where it happens. A closed trade is one round trip from flat
    back to flat. Apply fills oldest first (ledger_from() sorts them)."""

    def __init__(self):
        self.lots = defaultdict(deque)
        self.open_trades = {}
        self.trades = []
        self.realizations = []

    def apply(self, fill, expired=False):
        if fill["qty"] <= 0 or not fill["symbol"]:
            return
        symbol, price = fill["symbol"], fill["price"]
        mult = multiplier(symbol)
        lots, trade = self.lots[symbol], self.open_trades.get(symbol)
        fee = abs(fill.get("fee") or ZERO)
        charged = fee
        remaining = fill["qty"] if fill["side"] == "buy" else -fill["qty"]
        realized = cost = ZERO
        while remaining and lots and (lots[0].qty > 0) != (remaining > 0):
            lot = lots[0]
            direction = 1 if lot.qty > 0 else -1
            matched = min(abs(lot.qty), abs(remaining))
            part = (price - lot.price) * matched * mult * direction
            trade.pnl += part
            realized += part
            cost += abs(lot.price) * matched * mult
            lot.qty -= matched * direction
            remaining += matched * direction
            if lot.qty == 0:
                lots.popleft()
            if not lots:                          # back to flat: the round trip is done
                trade.pnl -= fee                   # the closing fill's fee belongs to the trade it closes
                realized -= fee
                fee = ZERO
                trade.closed_at = fill["time"]
                trade.expired = expired
                self.trades.append(trade)
                del self.open_trades[symbol]
                trade = None
        if remaining:
            if trade is None:
                trade = ClosedTrade(symbol=symbol, opened_at=fill["time"], closed_at=None,
                                    direction="long" if remaining > 0 else "short", quantity=ZERO)
                self.open_trades[symbol] = trade
            lots.append(Lot(remaining, price, fill["time"]))
            trade.quantity = max(trade.quantity, abs(sum(lot.qty for lot in lots)))
        if fee:                                   # an opening (or partial) fill's fee: charged to the open trade
            trade.pnl -= fee
            realized -= fee
        if cost or charged:
            self.realizations.append(Realization(fill["time"], symbol, realized, cost))

    def expire(self, as_of, held=()):
        """Closes at $0 every option still open whose expiration is before `as_of` (a date) and that the
        account no longer holds (`held`: symbols in the account's current positions)."""
        for symbol, lots in list(self.lots.items()):
            occ = parse_occ(symbol)
            if not lots or symbol in held or not occ or occ["expiration"] >= as_of:
                continue
            qty = sum(lot.qty for lot in lots)
            when = timezone.make_aware(datetime.combine(occ["expiration"], time(16, 0)))
            self.apply({"id": f"expired-{symbol}", "symbol": symbol, "side": "sell" if qty > 0 else "buy",
                        "qty": abs(qty), "price": ZERO, "time": when, "fee": ZERO}, expired=True)

    @property
    def realized_total(self):
        return sum((r.pnl for r in self.realizations), ZERO)

    def open_positions(self):
        """{symbol: {"qty" (signed), "avg_price", "lots", "opened_at"}} for every contract still open."""
        positions = {}
        for symbol, lots in self.lots.items():
            qty = sum((lot.qty for lot in lots), ZERO)
            if not lots or not qty:
                continue
            size = sum((abs(lot.qty) for lot in lots), ZERO)
            positions[symbol] = {"qty": qty, "lots": list(lots), "opened_at": self.open_trades[symbol].opened_at,
                                 "avg_price": sum((abs(lot.qty) * lot.price for lot in lots), ZERO) / size}
        return positions


def _ordered(fills):
    return sorted(fills, key=lambda f: (f["time"] or _EARLIEST, f["id"]))


def ledger_from(fills, as_of=None, held=()):
    """A FifoLedger with these fills applied (oldest first), and expired options closed as of `as_of`."""
    ledger = FifoLedger()
    for fill in _ordered(fills):
        ledger.apply(fill)
    if as_of:
        ledger.expire(as_of, held)
    return ledger


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def profit_factor(trades):
    """Gross wins / gross losses. None when there are no losing trades (shown as "-", or "∞" with wins)."""
    wins = sum((t.pnl for t in trades if t.pnl > 0), ZERO)
    losses = -sum((t.pnl for t in trades if t.pnl < 0), ZERO)
    return (wins / losses).quantize(CENT) if losses else None


def max_drawdown(values, capital=None):
    """Largest fall from a running peak in a cumulative P&L series (starting value included).
    Returns (amount >= 0, % of the equity at that peak - capital + peak P&L - or None without capital)."""
    peak, worst, worst_pct = None, ZERO, None
    for value in values:
        value = Decimal(value)
        peak = value if peak is None else max(peak, value)
        drop = peak - value
        worst = max(worst, drop)
        if capital:
            base = Decimal(capital) + peak
            if base > 0 and drop > 0:
                pct = drop / base * 100
                worst_pct = pct if worst_pct is None else max(worst_pct, pct)
    if capital and worst_pct is None:
        worst_pct = ZERO
    return worst.quantize(CENT), (worst_pct.quantize(CENT) if worst_pct is not None else None)


def _stats(trades):
    stats = trade_stats(trades)
    stats["profit_factor"] = profit_factor(trades)
    stats["gross_wins"] = any(t.pnl > 0 for t in trades)
    return stats


# ---------------------------------------------------------------------------
# One bucket's numbers (a strategy, or Unattributed)
# ---------------------------------------------------------------------------

def bucket_report(ref, fills, account_positions, start=None, today=None, price_at=lambda symbol: None):
    """The comparison-table row (and detail page) for one strategy, from its own fills.

    account_positions: {symbol: the account's position row} - current price, yesterday's close
    (lastday_price), and which contracts are still held. Unrealized P&L per open contract is
    (current - its own cost) x its own quantity x multiplier. Today's P&L = what its fills realized today +
    the intraday change of its open lots (from yesterday's close, or from cost for lots opened today).
    Total P&L for the period = realized in the period + the change in unrealized since the period's start
    (price_at(symbol) for lots opened before it; without that price the full unrealized P&L counts and
    the row is flagged "before")."""
    today = today or timezone.localdate()
    ledger = ledger_from(fills, as_of=today, held=set(account_positions))
    realizations = sorted(ledger.realizations, key=lambda r: r.time or _EARLIEST)
    notes = set()
    open_rows = []
    unrealized_total = intraday = period_unrealized = ZERO
    for symbol, position in sorted(ledger.open_positions().items()):
        account = account_positions.get(symbol) or {}
        current, lastday = account.get("current_price"), account.get("lastday_price")
        mult = multiplier(symbol)
        unrealized = None
        if current is None:
            notes.add("price")
        else:
            unrealized = sum(((current - lot.price) * lot.qty * mult for lot in position["lots"]), ZERO)
            unrealized_total += unrealized
            for lot in position["lots"]:
                opened_today = _local_date(lot.time) == today
                reference = lot.price if opened_today or lastday is None else lastday
                intraday += (current - reference) * lot.qty * mult
                if start is None or (lot.time and lot.time >= start):
                    period_unrealized += (current - lot.price) * lot.qty * mult
                    continue
                start_price = price_at(symbol)
                if start_price is None:
                    period_unrealized += (current - lot.price) * lot.qty * mult
                    notes.add("before")
                else:
                    period_unrealized += (current - start_price) * lot.qty * mult
        open_rows.append({
            "symbol": symbol, "description": describe_contract(symbol) if parse_occ(symbol) else "",
            "qty": position["qty"], "avg_price": position["avg_price"].quantize(Decimal("0.0001")),
            "current_price": current, "unrealized_pl": unrealized.quantize(CENT) if unrealized is not None else None,
            "dte": days_to_expiration(symbol, today), "opened_at": position["opened_at"], "shared_with": [],
            "account_qty": account.get("qty"),
        })
    realized_today = sum((r.pnl for r in realizations if _local_date(r.time) == today), ZERO)
    period_realizations = [r for r in realizations if start is None or (r.time and r.time >= start)]
    period_realized = sum((r.pnl for r in period_realizations), ZERO)
    trades = sorted((t for t in ledger.trades if start is None or (t.closed_at and t.closed_at >= start)),
                    key=lambda t: t.closed_at or _EARLIEST, reverse=True)
    curve, running = [ZERO], ZERO
    for realization in period_realizations:
        running += realization.pnl
        curve.append(running)
    curve.append(running + period_unrealized)
    total = (period_realized + period_unrealized).quantize(CENT)
    drawdown, drawdown_pct = max_drawdown(curve, ref.capital)
    return {
        "ref": ref, "key": ref.key, "name": ref.name, "color": ref.color, "status": ref.status,
        "today_pl": (realized_today + intraday).quantize(CENT), "total_pl": total,
        "return_pct": (total / ref.capital * 100).quantize(CENT) if ref.capital else None,
        "stats": _stats(trades), "trades": trades, "open_positions": open_rows, "open_count": len(open_rows),
        "max_drawdown": drawdown, "max_drawdown_pct": drawdown_pct,
        "realized_to_date": ledger.realized_total.quantize(CENT), "unrealized": unrealized_total.quantize(CENT),
        "trades_closed_today": sum(1 for t in ledger.trades if _local_date(t.closed_at) == today),
        "realizations": realizations, "period_realizations": period_realizations,
        "period_unrealized": period_unrealized, "notes": notes, "fill_count": len(fills),
    }


def total_report(rows, capital=None):
    """The Total row: every bucket together. Money adds up; win rate, averages and profit factor are over
    every closed trade; drawdown is on all the buckets' realized P&L merged in time order."""
    trades = [t for row in rows for t in row["trades"]]
    realizations = sorted((r for row in rows for r in row["period_realizations"]), key=lambda r: r.time or _EARLIEST)
    curve, running = [ZERO], ZERO
    for realization in realizations:
        running += realization.pnl
        curve.append(running)
    curve.append(running + sum((row["period_unrealized"] for row in rows), ZERO))
    drawdown, drawdown_pct = max_drawdown(curve, capital)
    total = sum((row["total_pl"] for row in rows), ZERO)
    return {
        "key": "total", "name": "Total", "color": "", "status": "",
        "today_pl": sum((row["today_pl"] for row in rows), ZERO), "total_pl": total,
        "return_pct": (total / capital * 100).quantize(CENT) if capital else None,
        "stats": _stats(trades), "trades": trades,
        "open_count": len({p["symbol"] for row in rows for p in row["open_positions"]}),
        "max_drawdown": drawdown, "max_drawdown_pct": drawdown_pct,
        "notes": set().union(*(row["notes"] for row in rows)) if rows else set(),
    }


def mark_shared(rows):
    """Notes "shared contract" on each open position another bucket also holds. Returns {symbol: [keys]}."""
    holders = defaultdict(list)
    for row in rows:
        for position in row["open_positions"]:
            holders[position["symbol"]].append(row["key"])
    names = {row["key"]: row["name"] for row in rows}
    for row in rows:
        for position in row["open_positions"]:
            position["shared_with"] = [names[k] for k in holders[position["symbol"]] if k != row["key"]]
    return {symbol: keys for symbol, keys in holders.items() if len(keys) > 1}


def position_holders(rows):
    """{symbol: [(row, its signed qty), ...]} - which buckets' fills hold each contract."""
    holders = defaultdict(list)
    for row in rows:
        for position in row["open_positions"]:
            holders[position["symbol"]].append((row, position["qty"]))
    return holders


# ---------------------------------------------------------------------------
# History: the snapshot backfill and the chart
# ---------------------------------------------------------------------------

def daily_history(fills, end_date):
    """[(date, realized P&L to date, open positions at the day's end, trades closed that day)] for every day
    from the first fill's date to end_date - rebuilt from the fills alone (unrealized P&L can't be)."""
    ordered = _ordered([f for f in fills if f["time"]])
    if not ordered:
        return []
    ledger, index, day, history = FifoLedger(), 0, _local_date(ordered[0]["time"]), []
    closed_counted = 0
    while day <= end_date:
        while index < len(ordered) and _local_date(ordered[index]["time"]) <= day:
            ledger.apply(ordered[index])
            index += 1
        ledger.expire(day + timedelta(days=1))
        closed_today = len(ledger.trades) - closed_counted
        closed_counted = len(ledger.trades)
        history.append((day, ledger.realized_total.quantize(CENT), len(ledger.open_positions()), closed_today))
        day += timedelta(days=1)
    return history


def cumulative_series(realizations, first_day, today, live_value, snapshots=None, baseline_day=None):
    """[(date, cumulative P&L)] for each day first_day..today, measured from the end of baseline_day
    (0 when None). A day's value is its snapshot's realized + unrealized P&L when one exists, else the
    realized P&L to date from the fills; today's is live_value (realized to date + current unrealized)."""
    snapshots = snapshots or {}
    ordered = sorted((r for r in realizations if r.time), key=lambda r: r.time)
    by_day = Counter()
    for realization in ordered:
        by_day[_local_date(realization.time)] += realization.pnl
    days_with_pnl = sorted(by_day)

    def realized_to(day):
        return sum((by_day[d] for d in days_with_pnl if d <= day), ZERO)

    def value(day):
        if day == today:
            return live_value
        snap = snapshots.get(day)
        return snap if snap is not None else realized_to(day)

    base = value(baseline_day) if baseline_day else ZERO
    points, day = [], first_day
    while day <= today:
        points.append((day, (value(day) - base).quantize(CENT)))
        day += timedelta(days=1)
    return points
