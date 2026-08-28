import datetime as dt
import logging
import time

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.trading.requests import MarketOrderRequest

from albright_trading_app.models import AlpacaCredentials, Strategy, Trade, OptionStrategy, OptionTrade
from albright_trading_app.strategy_framework import STRATEGY_REGISTRY
from albright_trading_app.strategy_filters import resolve_strategy_symbols, get_sentiment_for_symbol
from albright_trading_app.views import get_bars_for_symbol, resolve_option_strategy_symbols
from albright_trading_app.options_trading import (
    get_atm_option_contract,
    get_current_option_prices,
    MINIMUM_DTE_BEFORE_FORCE_CLOSE,
)

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 300
MARKET_CLOSED_SLEEP_SECONDS = 900


class Command(BaseCommand):
    help = (
        "Always-on strategy engine. Evaluates active stock strategies and "
        "active option strategies every pass during market hours, each "
        "through its own independent code path."
    )

    def handle(self, *args, **options):
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

                self._run_stock_pass()
                self._run_option_pass()

            except Exception:
                logger.exception("Error during strategy engine pass")

            time.sleep(CHECK_INTERVAL_SECONDS)

    # ============================================================
    # STOCK STRATEGIES — unchanged from before options existed
    # ============================================================

    def _run_stock_pass(self):
        for strategy in Strategy.objects.filter(is_active=True).select_related("user"):
            try:
                self._evaluate_strategy(strategy)
            except Exception:
                logger.exception("Error evaluating stock strategy '%s'", strategy.name)

    def _evaluate_strategy(self, strategy):
        try:
            credentials = strategy.user.alpaca_credentials
        except AlpacaCredentials.DoesNotExist:
            logger.warning(
                "Stock strategy '%s' (user: %s) is active but the owner hasn't connected "
                "an Alpaca account — skipping.", strategy.name, strategy.user.username,
            )
            return

        if not credentials.is_paper:
            logger.warning(
                "Stock strategy '%s' (user: %s) is active but the owner's connected account "
                "is LIVE, not paper. Live execution isn't enabled yet — skipping.",
                strategy.name, strategy.user.username,
            )
            return

        trading_client = TradingClient(
            api_key=credentials.get_api_key(),
            secret_key=credentials.get_secret_key(),
            paper=credentials.is_paper,
        )

        self._check_open_trades_for_exit(strategy, trading_client)

        strategy_class = STRATEGY_REGISTRY.get(strategy.strategy_type)
        if strategy_class is None:
            logger.warning("Unknown strategy_type '%s' for '%s' — skipping.", strategy.strategy_type, strategy.name)
            return

        strategy_instance = strategy_class(params=strategy.parameters)

        try:
            symbols = resolve_strategy_symbols(strategy)
        except Exception:
            logger.exception("Failed to resolve symbol universe for '%s'", strategy.name)
            return

        self.stdout.write(
            f"[{strategy.user.username}] {strategy.name}: universe resolved to "
            f"{len(symbols)} symbol(s): {', '.join(symbols) if symbols else '(none)'}"
        )

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

            self.stdout.write(
                f"[{strategy.user.username}] {strategy.name}: {symbol} -> {signal} "
                f"(mentions_24h={sentiment.get('mentions_24h')}, "
                f"positive_ratio_24h={sentiment.get('positive_ratio_24h', 0):.2f})"
            )

            if signal == "hold":
                continue

            open_trade = strategy.trades.filter(symbol=symbol, status="open").first()

            if signal == "buy" and open_trade is not None:
                self.stdout.write(f"  -> skipped: already holding an open position in {symbol}")
                continue
            if signal == "sell" and open_trade is None:
                self.stdout.write(f"  -> skipped: sell signal but no open position in {symbol}")
                continue

            if signal == "buy" and strategy.max_daily_entries_per_symbol is not None:
                today = timezone.localdate()
                entries_today = strategy.trades.filter(
                    symbol=symbol, side="buy", entered_at__date=today,
                ).count()
                if entries_today >= strategy.max_daily_entries_per_symbol:
                    self.stdout.write(
                        f"  -> skipped: daily entry cap reached for {symbol} "
                        f"({entries_today}/{strategy.max_daily_entries_per_symbol})"
                    )
                    continue

            last_close = bars[-1]["close"]
            self._place_signal_order(strategy, symbol, signal, open_trade, trading_client, last_close)

    def _check_open_trades_for_exit(self, strategy, trading_client):
        for trade in strategy.trades.filter(status="open"):
            try:
                bars = get_bars_for_symbol(trade.symbol, "1D")
            except Exception:
                logger.exception("Failed to fetch price for open trade %s (%s)", trade.symbol, strategy.name)
                continue

            if not bars:
                continue

            current_price = bars[-1]["close"]

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
            return max(0, int((buying_power * (value / 100)) // last_close))
        if method == "pct_cash":
            try:
                account = trading_client.get_account()
                cash = float(account.cash)
            except Exception:
                logger.exception("Failed to fetch cash balance for '%s'", strategy.name)
                return 0
            return max(0, int((cash * (value / 100)) // last_close))

        logger.warning("Unknown position_sizing_method '%s' for '%s'", method, strategy.name)
        return 0

    def _place_signal_order(self, strategy, symbol, signal, open_trade, trading_client, last_close):
        if signal == "buy":
            qty = self._compute_order_quantity(strategy, trading_client, last_close)
            if qty <= 0:
                self.stdout.write(f"  -> skipped: computed order quantity was 0 for {symbol}")
                return
        else:
            qty = open_trade.quantity

        order_request = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if signal == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )

        try:
            order = trading_client.submit_order(order_request)
        except Exception:
            logger.exception("Order submission failed: %s %s x%d (%s)", signal, symbol, qty, strategy.name)
            return

        if signal == "buy":
            Trade.objects.create(
                strategy=strategy, symbol=symbol, side="buy", quantity=qty,
                entry_price=last_close, peak_price=last_close,
                alpaca_order_id=str(order.id), status="open",
            )
            self.stdout.write(f"[{strategy.user.username}] {strategy.name}: BUY {qty} {symbol} @ ~${last_close:.2f}")
        else:
            open_trade.exit_price = last_close
            open_trade.realized_pnl = (last_close - open_trade.entry_price) * open_trade.quantity
            open_trade.status = "closed"
            open_trade.exited_at = timezone.now()
            open_trade.save()
            self.stdout.write(
                f"[{strategy.user.username}] {strategy.name}: SELL {qty} {symbol} @ ~${last_close:.2f} "
                f"(P&L ${open_trade.realized_pnl:.2f})"
            )

    def _close_trade(self, strategy, trade, trading_client, exit_price, reason):
        order_request = MarketOrderRequest(
            symbol=trade.symbol, qty=trade.quantity, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        )
        try:
            trading_client.submit_order(order_request)
        except Exception:
            logger.exception("Exit order failed for %s (%s, reason: %s)", trade.symbol, strategy.name, reason)
            return

        trade.exit_price = exit_price
        trade.realized_pnl = (exit_price - trade.entry_price) * trade.quantity
        trade.status = "closed"
        trade.exited_at = timezone.now()
        trade.save()

        self.stdout.write(
            f"[{strategy.user.username}] {strategy.name}: EXIT ({reason}) {trade.quantity} {trade.symbol} "
            f"@ ~${exit_price:.2f} (P&L ${trade.realized_pnl:.2f})"
        )

    # ============================================================
    # OPTION STRATEGIES — fully independent path, own model, own
    # Trade equivalent, no shared state with the stock path above
    # beyond low-level data utilities.
    # ============================================================

    def _run_option_pass(self):
        for strategy in OptionStrategy.objects.filter(is_active=True).select_related("user"):
            try:
                self._evaluate_option_strategy(strategy)
            except Exception:
                logger.exception("Error evaluating option strategy '%s'", strategy.name)

    def _evaluate_option_strategy(self, strategy):
        try:
            credentials = strategy.user.alpaca_credentials
        except AlpacaCredentials.DoesNotExist:
            logger.warning(
                "Option strategy '%s' (user: %s) is active but the owner hasn't connected "
                "an Alpaca account — skipping.", strategy.name, strategy.user.username,
            )
            return

        if not credentials.is_paper:
            logger.warning(
                "Option strategy '%s' (user: %s) is active but the owner's connected account "
                "is LIVE, not paper. Live execution isn't enabled yet — skipping.",
                strategy.name, strategy.user.username,
            )
            return

        trading_client = TradingClient(
            api_key=credentials.get_api_key(),
            secret_key=credentials.get_secret_key(),
            paper=credentials.is_paper,
        )

        self._check_open_option_trades_for_exit(strategy, trading_client)

        strategy_class = STRATEGY_REGISTRY.get(strategy.strategy_type)
        if strategy_class is None:
            logger.warning("Unknown strategy_type '%s' for '%s' — skipping.", strategy.strategy_type, strategy.name)
            return

        strategy_instance = strategy_class(params=strategy.parameters)

        try:
            symbols = resolve_option_strategy_symbols(strategy)
        except Exception:
            logger.exception("Failed to resolve symbol universe for '%s'", strategy.name)
            return

        self.stdout.write(
            f"[{strategy.user.username}] {strategy.name} (options): universe resolved to "
            f"{len(symbols)} symbol(s): {', '.join(symbols) if symbols else '(none)'}"
        )

        for underlying_symbol in symbols:
            try:
                bars = get_bars_for_symbol(underlying_symbol, "1D")
            except Exception:
                logger.exception("Failed to fetch bars for %s while evaluating '%s'", underlying_symbol, strategy.name)
                continue

            if not bars:
                continue

            sentiment = get_sentiment_for_symbol(underlying_symbol)
            signal = strategy_instance.generate_signal(underlying_symbol, bars, sentiment=sentiment)
            self.stdout.write(f"[{strategy.user.username}] {strategy.name}: {underlying_symbol} -> {signal}")

            if signal == "hold":
                continue

            option_type = "call" if signal == "buy" else "put"

            existing = strategy.trades.filter(
                underlying_symbol=underlying_symbol, option_type=option_type, status="open",
            ).first()
            if existing is not None:
                self.stdout.write(f"  -> skipped: already holding an open {option_type} on {underlying_symbol}")
                continue

            if strategy.max_daily_entries_per_symbol is not None:
                today = timezone.localdate()
                entries_today = strategy.trades.filter(
                    underlying_symbol=underlying_symbol, option_type=option_type, entered_at__date=today,
                ).count()
                if entries_today >= strategy.max_daily_entries_per_symbol:
                    self.stdout.write(
                        f"  -> skipped: daily entry cap reached for {underlying_symbol} {option_type}s "
                        f"({entries_today}/{strategy.max_daily_entries_per_symbol})"
                    )
                    continue

            last_close = bars[-1]["close"]
            self._enter_option_position(strategy, underlying_symbol, option_type, last_close, trading_client)

    def _enter_option_position(self, strategy, underlying_symbol, option_type, current_price, trading_client):
        contract = get_atm_option_contract(
            underlying_symbol, option_type, strategy.option_target_dte, current_price
        )
        if contract is None:
            self.stdout.write(f"  -> skipped: no suitable {option_type} contract found for {underlying_symbol}")
            return

        qty = self._compute_option_quantity(strategy, trading_client, contract["premium"])
        if qty <= 0:
            self.stdout.write(
                f"  -> skipped: computed contract quantity was 0 for {underlying_symbol} "
                f"(premium ${contract['premium']:.2f})"
            )
            return

        order_request = MarketOrderRequest(
            symbol=contract["contract_symbol"], qty=qty, side=OrderSide.BUY, time_in_force=TimeInForce.DAY,
        )

        try:
            order = trading_client.submit_order(order_request)
        except Exception:
            logger.exception(
                "Option order submission failed: BUY %s x%d (%s)",
                contract["contract_symbol"], qty, strategy.name,
            )
            return

        OptionTrade.objects.create(
            strategy=strategy,
            symbol=contract["contract_symbol"],
            underlying_symbol=underlying_symbol,
            option_type=option_type,
            strike_price=contract["strike"],
            expiration_date=contract["expiration_date"],
            quantity=qty,
            entry_price=contract["premium"],
            peak_price=contract["premium"],
            alpaca_order_id=str(order.id),
            status="open",
        )

        self.stdout.write(
            f"[{strategy.user.username}] {strategy.name}: BUY {qty}x {option_type.upper()} "
            f"{underlying_symbol} ${contract['strike']} exp {contract['expiration_date']} "
            f"@ ~${contract['premium']:.2f}"
        )

    def _compute_option_quantity(self, strategy, trading_client, premium):
        if premium <= 0:
            return 0

        method = strategy.position_sizing_method
        value = strategy.position_sizing_value
        cost_per_contract = premium * 100

        if method == "fixed_contracts":
            return max(1, int(value))
        if method == "fixed_dollar":
            return max(0, int(value // cost_per_contract))
        if method in ("pct_buying_power", "pct_cash"):
            try:
                account = trading_client.get_account()
                base = float(account.buying_power) if method == "pct_buying_power" else float(account.cash)
            except Exception:
                logger.exception("Failed to fetch account balance for '%s'", strategy.name)
                return 0
            return max(0, int((base * (value / 100)) // cost_per_contract))

        logger.warning("Unknown position_sizing_method '%s' for '%s'", method, strategy.name)
        return 0

    def _check_open_option_trades_for_exit(self, strategy, trading_client):
        open_trades = strategy.trades.filter(status="open")
        if not open_trades.exists():
            return

        current_prices = get_current_option_prices([t.symbol for t in open_trades])

        for trade in open_trades:
            days_to_expiration = (trade.expiration_date - dt.date.today()).days
            current_price = current_prices.get(trade.symbol)
            exit_reason = None

            if days_to_expiration <= MINIMUM_DTE_BEFORE_FORCE_CLOSE:
                exit_reason = "near expiration"

            if exit_reason is None and current_price is not None:
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
                price_to_use = current_price if current_price is not None else trade.entry_price
                self._close_option_trade(strategy, trade, trading_client, price_to_use, exit_reason)

    def _close_option_trade(self, strategy, trade, trading_client, exit_price, reason):
        order_request = MarketOrderRequest(
            symbol=trade.symbol, qty=trade.quantity, side=OrderSide.SELL, time_in_force=TimeInForce.DAY,
        )
        try:
            trading_client.submit_order(order_request)
        except Exception:
            logger.exception("Option exit order failed for %s (%s, reason: %s)", trade.symbol, strategy.name, reason)
            return

        trade.exit_price = exit_price
        trade.realized_pnl = (exit_price - trade.entry_price) * trade.quantity * 100
        trade.status = "closed"
        trade.exited_at = timezone.now()
        trade.save()

        self.stdout.write(
            f"[{strategy.user.username}] {strategy.name}: EXIT ({reason}) {trade.quantity}x "
            f"{trade.option_type.upper()} {trade.underlying_symbol} @ ~${exit_price:.2f} "
            f"(P&L ${trade.realized_pnl:.2f})"
        )