import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from albright_trading_app.models import AlpacaCredentials, Strategy, Trade
from albright_trading_app.strategy_framework import STRATEGY_REGISTRY
from albright_trading_app.strategy_filters import resolve_strategy_symbols, get_sentiment_for_symbol
from albright_trading_app.views import get_bars_for_symbol

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300  # 5 minutes between passes
MARKET_CLOSED_SLEEP_SECONDS = 900  # check less often when the market's shut


class Command(BaseCommand):
    help = (
        "Always-on strategy engine. Intended to run as a PythonAnywhere "
        "Always-on Task — loops forever, checking open positions for "
        "risk-management exits and evaluating active strategies for new "
        "entries, all through each strategy owner's own Alpaca account."
    )

    def handle(self, *args, **options):
        # Global keys ONLY for checking market hours — that's not
        # account-specific. Order placement below always uses each
        # strategy owner's own per-user credentials.
        clock_client = TradingClient(
            settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY, paper=settings.ALPACA_PAPER
        )

        self.stdout.write(self.style.SUCCESS("Strategy engine starting..."))

        while True:
            try:
                clock = clock_client.get_clock()

                if not clock.is_open:
                    self.stdout.write(f"Market closed. Next open: {clock.next_open}. Sleeping.")
                    time.sleep(MARKET_CLOSED_SLEEP_SECONDS)
                    continue

                self._run_pass()

            except Exception:
                logger.exception("Error during strategy engine pass")

            time.sleep(CHECK_INTERVAL_SECONDS)

    def _run_pass(self):
        for strategy in Strategy.objects.filter(is_active=True).select_related("user"):
            try:
                self._evaluate_strategy(strategy)
            except Exception:
                logger.exception("Error evaluating strategy '%s'", strategy.name)

    def _evaluate_strategy(self, strategy):
        try:
            credentials = strategy.user.alpaca_credentials
        except AlpacaCredentials.DoesNotExist:
            logger.warning(
                "Strategy '%s' (user: %s) is active but the owner hasn't connected "
                "an Alpaca account — skipping.", strategy.name, strategy.user.username,
            )
            return

        if not credentials.is_paper:
            logger.warning(
                "Strategy '%s' (user: %s) is active but the owner's connected account "
                "is LIVE, not paper. Live execution isn't enabled yet — skipping.",
                strategy.name, strategy.user.username,
            )
            return

        trading_client = TradingClient(
            api_key=credentials.get_api_key(),
            secret_key=credentials.get_secret_key(),
            paper=credentials.is_paper,
        )

        # ---- Step 1: risk management on positions already open ----
        # Checked for every open trade regardless of whether that
        # symbol still matches today's filters — you still own the
        # shares even if the stock has since fallen out of criteria.
        self._check_open_trades_for_exit(strategy, trading_client)

        # ---- Step 2: new entries on the current eligible universe ----
        strategy_class = STRATEGY_REGISTRY.get(strategy.strategy_type)
        if strategy_class is None:
            logger.warning(
                "Unknown strategy_type '%s' for strategy '%s' — skipping.",
                strategy.strategy_type, strategy.name,
            )
            return

        strategy_instance = strategy_class(params=strategy.parameters)

        try:
            symbols = resolve_strategy_symbols(strategy)
        except Exception:
            logger.exception("Failed to resolve symbol universe for '%s'", strategy.name)
            return

        for symbol in symbols:
            try:
                bars = get_bars_for_symbol(symbol, "1D")
            except Exception:
                logger.exception("Failed to fetch bars for %s while evaluating '%s'", symbol, strategy.name)
                continue

            if not bars:
                continue

            sentiment = get_sentiment_for_symbol(symbol)
            signal = strategy_instance.generate_signal(symbol, bars, sentiment=sentiment)
            if signal == "hold":
                continue

            open_trade = strategy.trades.filter(symbol=symbol, status="open").first()

            if signal == "buy" and open_trade is not None:
                continue
            if signal == "sell" and open_trade is None:
                continue

            if signal == "buy" and strategy.max_daily_entries_per_symbol is not None:
                # Count every BUY entered today for this symbol, whether
                # it's still open or already closed — a fast take-profit
                # round-trip still counts as one of today's entries, which
                # is exactly the case this cap exists to prevent.
                today = timezone.localdate()
                entries_today = strategy.trades.filter(
                    symbol=symbol,
                    side="buy",
                    entered_at__date=today,
                ).count()

                if entries_today >= strategy.max_daily_entries_per_symbol:
                    continue  # daily entry cap reached for this symbol

            last_close = bars[-1]["close"]
            self._place_signal_order(strategy, symbol, signal, open_trade, trading_client, last_close)

    def _check_open_trades_for_exit(self, strategy, trading_client):
        for trade in strategy.trades.filter(status="open"):
            try:
                bars = get_bars_for_symbol(trade.symbol, "1D")
            except Exception:
                logger.exception(
                    "Failed to fetch price for open trade %s (%s)", trade.symbol, strategy.name
                )
                continue

            if not bars:
                continue

            current_price = bars[-1]["close"]

            # Trailing stop tracks the highest price seen since entry —
            # update it BEFORE checking the exit condition, so a bar
            # that makes a new high never immediately triggers its own
            # trailing stop.
            if trade.peak_price is None or current_price > trade.peak_price:
                trade.peak_price = current_price
                trade.save(update_fields=["peak_price"])

            pct_change = (
                (current_price - trade.entry_price) / trade.entry_price * 100
                if trade.entry_price else 0
            )
            pct_from_peak = (
                (current_price - trade.peak_price) / trade.peak_price * 100
                if trade.peak_price else 0
            )

            exit_reason = None
            if strategy.take_profit_pct is not None and pct_change >= strategy.take_profit_pct:
                exit_reason = "take profit"
            elif strategy.stop_loss_pct is not None and pct_change <= -strategy.stop_loss_pct:
                exit_reason = "stop loss"
            elif strategy.trailing_stop_pct is not None and pct_from_peak <= -strategy.trailing_stop_pct:
                exit_reason = "trailing stop"
            elif strategy.max_hold_days is not None:
                days_held = (timezone.now() - trade.entered_at).days
                if days_held >= strategy.max_hold_days:
                    exit_reason = "max hold period"

            if exit_reason:
                self._close_trade(strategy, trade, trading_client, current_price, exit_reason)

    def _compute_order_quantity(self, strategy, trading_client, last_close):
        if last_close <= 0:
            return 0

        method = strategy.position_sizing_method
        value = strategy.position_sizing_value

        if method == "fixed_shares":
            return max(1, int(value))

        if method == "fixed_dollar":
            return max(0, int(value // last_close))

        if method == "pct_buying_power":
            try:
                account = trading_client.get_account()
                buying_power = float(account.buying_power)
            except Exception:
                logger.exception("Failed to fetch buying power for '%s'", strategy.name)
                return 0
            dollar_amount = buying_power * (value / 100)
            return max(0, int(dollar_amount // last_close))

        if method == "pct_cash":
            try:
                account = trading_client.get_account()
                cash = float(account.cash)
            except Exception:
                logger.exception("Failed to fetch cash balance for '%s'", strategy.name)
                return 0
            dollar_amount = cash * (value / 100)
            return max(0, int(dollar_amount // last_close))

        logger.warning("Unknown position_sizing_method '%s' for '%s'", method, strategy.name)
        return 0

    def _place_signal_order(self, strategy, symbol, signal, open_trade, trading_client, last_close):
        if signal == "buy":
            qty = self._compute_order_quantity(strategy, trading_client, last_close)
            if qty <= 0:
                logger.warning(
                    "Computed order quantity was 0 for %s (%s) — skipping buy.", symbol, strategy.name
                )
                return
        else:
            qty = open_trade.quantity  # sell exactly what's held

        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if signal == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )

        try:
            order = trading_client.submit_order(order_request)
        except Exception:
            logger.exception(
                "Order submission failed: %s %s x%d (%s, user: %s)",
                signal, symbol, qty, strategy.name, strategy.user.username,
            )
            return

        if signal == "buy":
            Trade.objects.create(
                strategy=strategy,
                symbol=symbol,
                side="buy",
                quantity=qty,
                entry_price=last_close,
                peak_price=last_close,
                alpaca_order_id=str(order.id),
                status="open",
            )
            self.stdout.write(
                f"[{strategy.user.username}] {strategy.name}: BUY {qty} {symbol} @ ~${last_close:.2f}"
            )
        else:
            open_trade.exit_price = last_close
            open_trade.realized_pnl = (last_close - open_trade.entry_price) * open_trade.quantity
            open_trade.status = "closed"
            open_trade.exited_at = timezone.now()
            open_trade.save()
            self.stdout.write(
                f"[{strategy.user.username}] {strategy.name}: SELL (signal) {qty} {symbol} "
                f"@ ~${last_close:.2f} (P&L ${open_trade.realized_pnl:.2f})"
            )

    def _close_trade(self, strategy, trade, trading_client, exit_price, reason):
        order_request = MarketOrderRequest(
            symbol=trade.symbol,
            qty=trade.quantity,
            side=OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )

        try:
            trading_client.submit_order(order_request)
        except Exception:
            logger.exception(
                "Exit order failed for %s (%s, reason: %s)", trade.symbol, strategy.name, reason
            )
            return

        trade.exit_price = exit_price
        trade.realized_pnl = (exit_price - trade.entry_price) * trade.quantity
        trade.status = "closed"
        trade.exited_at = timezone.now()
        trade.save()

        self.stdout.write(
            f"[{strategy.user.username}] {strategy.name}: EXIT ({reason}) {trade.quantity} "
            f"{trade.symbol} @ ~${exit_price:.2f} (P&L ${trade.realized_pnl:.2f})"
        )