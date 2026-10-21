"""The Grok Bot page (read-only monitor of the Alpaca paper account an
external bot trades in). Alpaca is mocked at the TradingClient class - no
test ever reaches the real API. Covers: the page rendering from mocked
data, OCC symbol parsing, realized P&L / win-rate math, the "Bot order ID
prefix" filter on and off, the view never touching an order-submission or
cancel method, the 60-second cache, and API errors / missing or live
credentials handled with a message instead of a broken page."""
import inspect
import re
from datetime import date, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_trading_app import grok_bot
from albright_trading_app.grok_bot import (ReadOnlyAlpaca, closed_trades, describe_contract, filter_fills,
                                           normalize_fill, order_ids_to_client_ids, parse_occ, trade_stats)
from albright_trading_app.models import AlpacaCredentials, GrokBotSettings

TRADING_CLIENT = "alpaca.trading.client.TradingClient"
HOME_OUTLOOK = "albright_trading_app.views.get_market_outlook"
# Every TradingClient method that could change anything in the account.
WRITE_METHODS = ("submit_order", "cancel_orders", "cancel_order_by_id", "replace_order_by_id", "close_position",
                 "close_all_positions", "exercise_options_position", "post", "patch", "put", "delete")
READ_METHODS = {"get_account", "get_all_positions", "get_orders", "get_portfolio_history", "get"}

NOW = timezone.now()
CALL = "SPY" + (date.today() + timedelta(days=10)).strftime("%y%m%d") + "C00500000"
PUT = "SPY" + (date.today() + timedelta(days=10)).strftime("%y%m%d") + "P00480000"


def fill(symbol, side, qty, price, minutes_ago, order_id="o1", fill_id=None):
    return {"id": fill_id or f"{symbol}-{side}-{minutes_ago}", "activity_type": "FILL", "symbol": symbol,
            "side": side, "qty": str(qty), "price": str(price), "order_id": order_id,
            "transaction_time": (NOW - timedelta(minutes=minutes_ago)).isoformat()}


def order(order_id, client_order_id, symbol="AAPL", side="buy", status="filled", legs=None, **extra):
    return SimpleNamespace(id=order_id, client_order_id=client_order_id, symbol=symbol, side=side, qty="1",
                           order_type="limit", type="limit", limit_price="2.50", status=status,
                           filled_avg_price="2.45" if status == "filled" else None, order_class="mleg" if legs else "simple",
                           legs=legs, submitted_at=NOW - timedelta(hours=1), created_at=NOW - timedelta(hours=1),
                           **extra)


def fake_client():
    client = mock.MagicMock(name="TradingClient()")
    client.get_account.return_value = SimpleNamespace(
        equity="105250.50", last_equity="104000.00", cash="80000", buying_power="160000",
        created_at=NOW - timedelta(days=400))
    client.get_all_positions.return_value = [
        SimpleNamespace(symbol=CALL, qty="2", side="long", avg_entry_price="3.10", current_price="4.00",
                        market_value="800", unrealized_pl="180", unrealized_plpc="0.2903"),
        SimpleNamespace(symbol="AAPL", qty="10", side="long", avg_entry_price="200", current_price="190",
                        market_value="1900", unrealized_pl="-100", unrealized_plpc="-0.05"),
    ]
    leg1 = SimpleNamespace(id="leg-a", symbol=CALL, side="buy", ratio_qty="1", client_order_id="auto-1")
    leg2 = SimpleNamespace(id="leg-b", symbol=PUT, side="sell", ratio_qty="1", client_order_id="auto-2")
    client.get_orders.return_value = [
        order("bot-1", "grok-123", symbol=CALL),
        order("mine-1", "manual-9", symbol="AAPL"),
        order("bot-2", "grok-124", symbol=None, side=None, legs=[leg1, leg2]),
        order("bot-3", "grok-125", symbol=CALL, status="canceled"),
    ]
    client.get_portfolio_history.return_value = SimpleNamespace(
        timestamp=[1760000000, 1760086400, 1760172800], equity=[100000.0, 103000.0, 105250.5],
        profit_loss=[0.0, 3000.0, 5250.5], profit_loss_pct=[0.0, 0.03, 0.0525], base_value=100000.0, timeframe="1D")
    client.get.return_value = [
        # Bot (order bot-1): buy 2 calls at 2.00, sell 2 at 3.50 -> +300 (x100)
        fill(CALL, "buy", 2, "2.00", 300, "bot-1"), fill(CALL, "sell", 2, "3.50", 200, "bot-1"),
        # Bot spread legs (order bot-2, reported by leg id): sell put 1.50, buy back 2.00 -> -50
        fill(PUT, "sell", 1, "1.50", 250, "leg-b"), fill(PUT, "buy", 1, "2.00", 100, "leg-b"),
        # Not the bot (order mine-1): AAPL 10 @ 190 -> 200 = +100
        fill("AAPL", "buy", 10, "190", 400, "mine-1"), fill("AAPL", "sell", 10, "200", 50, "mine-1"),
    ]
    return client


class OccParsingTests(TestCase):
    def test_parses_occ_symbols(self):
        occ = parse_occ("AAPL250117C00190000")
        self.assertEqual(occ, {"underlying": "AAPL", "expiration": date(2025, 1, 17), "type": "call",
                               "strike": Decimal("190")})
        self.assertEqual(parse_occ("SPY251121P00512500")["strike"], Decimal("512.5"))
        self.assertEqual(parse_occ("F260116P00001500")["strike"], Decimal("1.5"))
        self.assertEqual(parse_occ("BRK.B260116C00450000")["underlying"], "BRK.B")
        self.assertEqual(parse_occ("SPXW  251121P05800000")["underlying"], "SPXW")  # OCC padding

    def test_non_options_are_none(self):
        for symbol in ("AAPL", "", None, "AAPL250117X00190000", "AAPL251317C00190000", "TOOLONGNAME250117C00190000"):
            self.assertIsNone(parse_occ(symbol), symbol)

    def test_readable_description(self):
        self.assertEqual(describe_contract("AAPL250117C00190000"), "AAPL Jan 17, 2025 $190 call")
        self.assertEqual(describe_contract("SPY251121P00512500"), "SPY Nov 21, 2025 $512.5 put")
        self.assertEqual(describe_contract("AAPL"), "AAPL")

    def test_days_to_expiration(self):
        self.assertEqual(grok_bot.days_to_expiration("AAPL250117C00190000", today=date(2025, 1, 10)), 7)
        self.assertIsNone(grok_bot.days_to_expiration("AAPL"))


class RealizedPnlTests(TestCase):
    def trades(self, *fills):
        return closed_trades([normalize_fill(f) for f in fills])

    def test_long_option_round_trip_uses_the_100_multiplier(self):
        [t] = self.trades(fill(CALL, "buy", 2, "2.00", 30), fill(CALL, "sell", 2, "3.50", 20))
        self.assertEqual((t.pnl, t.direction, t.quantity), (Decimal("300.00"), "long", Decimal("2")))

    def test_stock_uses_no_multiplier(self):
        [t] = self.trades(fill("AAPL", "buy", 10, "190", 30), fill("AAPL", "sell", 10, "200", 20))
        self.assertEqual(t.pnl, Decimal("100"))

    def test_short_option_sell_to_open_buy_to_close(self):
        [t] = self.trades(fill(PUT, "sell", 3, "1.50", 30), fill(PUT, "buy", 3, "0.50", 20))
        self.assertEqual((t.pnl, t.direction), (Decimal("300.00"), "short"))

    def test_scale_in_and_partial_exits_are_one_trade_fifo(self):
        [t] = self.trades(fill(CALL, "buy", 1, "2.00", 50), fill(CALL, "buy", 1, "3.00", 40),
                          fill(CALL, "sell", 1, "2.50", 30), fill(CALL, "sell", 1, "2.50", 20))
        # FIFO: (2.50-2.00) + (2.50-3.00) = 0 -> x100
        self.assertEqual((t.pnl, t.quantity), (Decimal("0.00"), Decimal("2")))

    def test_flip_closes_and_opens(self):
        trades = self.trades(fill("AAPL", "buy", 5, "100", 50), fill("AAPL", "sell", 8, "110", 40),
                             fill("AAPL", "buy", 3, "105", 30))
        self.assertEqual([(t.direction, t.pnl) for t in trades], [("long", Decimal("50")), ("short", Decimal("15"))])

    def test_open_positions_are_not_closed_trades(self):
        self.assertEqual(self.trades(fill(CALL, "buy", 1, "2.00", 30)), [])

    def test_win_rate_and_averages(self):
        trades = self.trades(
            fill("A", "buy", 1, "10", 90), fill("A", "sell", 1, "15", 80),     # +5
            fill("B", "buy", 1, "10", 70), fill("B", "sell", 1, "7", 60),      # -3
            fill("C", "buy", 1, "10", 50), fill("C", "sell", 1, "11", 40),     # +1
            fill("D", "buy", 1, "10", 30), fill("D", "sell", 1, "10", 20))     # 0: a trade, not a win
        stats = trade_stats(trades)
        self.assertEqual(stats, {"count": 4, "wins": 2, "losses": 1, "win_rate": Decimal("50.0"),
                                 "avg_win": Decimal("3.00"), "avg_loss": Decimal("-3.00"), "total": Decimal("3.00")})

    def test_no_trades(self):
        self.assertEqual(trade_stats([])["win_rate"], None)


class PrefixFilterTests(TestCase):
    def test_legs_take_their_parents_client_order_id(self):
        ids = order_ids_to_client_ids(fake_client().get_orders())
        self.assertEqual((ids["bot-1"], ids["leg-b"], ids["mine-1"]), ("grok-123", "grok-124", "manual-9"))

    def test_filter_on_and_off(self):
        fills = [normalize_fill(f) for f in fake_client().get()]
        ids = order_ids_to_client_ids(fake_client().get_orders())
        self.assertEqual({f["symbol"] for f in filter_fills(fills, "grok-", ids)}, {CALL, PUT})
        self.assertEqual(len(filter_fills(fills, "", ids)), 6)
        unknown = normalize_fill(fill("XYZ", "buy", 1, "1", 5, "not-in-history"))
        self.assertEqual(filter_fills([unknown], "grok-", ids), [])


@mock.patch(HOME_OUTLOOK, return_value=None)
class GrokBotPageTests(TestCase):
    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.force_login(self.user)
        creds = AlpacaCredentials(user=self.user, is_paper=True)
        creds.set_credentials("PKTEST", "secret-test")
        creds.save()
        self.alpaca = fake_client()
        patcher = mock.patch(TRADING_CLIENT, return_value=self.alpaca)
        self.client_class = patcher.start()
        self.addCleanup(patcher.stop)

    def get(self, **params):
        return self.client.get(reverse("grok_bot"), params)

    def set_prefix(self, prefix):
        GrokBotSettings.objects.update_or_create(user=self.user, defaults={"order_id_prefix": prefix})

    def test_page_renders_from_mocked_data(self, _outlook):
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<title>Grok Bot · Albright Financial</title>")
        self.assertContains(response, "$105,250.50")                  # equity
        self.assertContains(response, "+$1,250.50")                   # today: 105,250.50 - 104,000
        self.assertContains(response, "+1.20% vs. yesterday")
        self.assertContains(response, "+$5,250.50")                   # total P&L from history
        self.assertContains(response, describe_contract(CALL))        # readable contract
        self.assertContains(response, '<td class="num">10</td>')      # DTE
        self.assertContains(response, "multi-leg")
        self.assertContains(response, "grok-124")
        self.assertContains(response, "lightweight-charts@4.1.3")
        self.assertEqual(response.context["chart_json"],
                         '[{"time": 1760000000, "value": 100000.0}, {"time": 1760086400, "value": 103000.0}, '
                         '{"time": 1760172800, "value": 105250.5}]')
        self.client_class.assert_called_once_with(api_key="PKTEST", secret_key="secret-test", paper=True)

    def test_no_prefix_shows_whole_account_with_note(self, _outlook):
        response = self.get()
        data = response.context["data"]
        self.assertFalse(data["bot_only"])
        self.assertEqual(len(data["orders"]), 4)
        self.assertEqual(data["stats"]["count"], 3)
        self.assertContains(response, "this includes all trades in this account")
        self.assertNotContains(response, 'id="scope-bot"')

    def test_prefix_filters_orders_fills_and_stats(self, _outlook):
        self.set_prefix("grok-")
        data = self.get().context["data"]
        self.assertTrue(data["bot_only"])
        self.assertEqual([o["client_order_id"] for o in data["orders"]], ["grok-123", "grok-124", "grok-125"])
        self.assertEqual(sorted(t.pnl for t in data["trades"]), [Decimal("-50.00"), Decimal("300.00")])
        self.assertEqual((data["stats"]["count"], data["stats"]["win_rate"]), (2, Decimal("50.0")))

    def test_whole_account_toggle_with_a_prefix(self, _outlook):
        self.set_prefix("grok-")
        response = self.get(scope="account")
        data = response.context["data"]
        self.assertFalse(data["bot_only"])
        self.assertEqual(len(data["orders"]), 4)
        self.assertEqual(data["stats"]["count"], 3)
        self.assertContains(response, 'class="scanner-tab is-active" id="scope-account"')

    def test_saving_the_prefix(self, _outlook):
        response = self.client.post(reverse("grok_bot"), {"order_id_prefix": "grok-"})
        self.assertRedirects(response, reverse("grok_bot"))
        self.assertEqual(GrokBotSettings.objects.get(user=self.user).order_id_prefix, "grok-")
        self.client.post(reverse("grok_bot"), {"order_id_prefix": ""})
        self.assertEqual(GrokBotSettings.objects.get(user=self.user).order_id_prefix, "")
        bad = self.client.post(reverse("grok_bot"), {"order_id_prefix": "grok <script>"})
        self.assertEqual(bad.status_code, 200)
        self.assertContains(bad, "Use letters, numbers")

    def test_period_toggles_request_the_right_history(self, _outlook):
        self.get(period="1Y")
        request = self.alpaca.get_portfolio_history.call_args.args[0]
        self.assertEqual((request.period, request.timeframe), ("1A", "1D"))
        cache.clear()
        self.get(period="all")
        request = self.alpaca.get_portfolio_history.call_args.args[0]
        self.assertIsNone(request.period)
        self.assertIsNotNone(request.start)
        cache.clear()
        self.get(period="1W")
        self.assertEqual(self.alpaca.get_portfolio_history.call_args.args[0].timeframe, "1H")

    def test_closed_trades_limited_to_the_period(self, _outlook):
        old = [fill("OLD", "buy", 1, "10", 60 * 24 * 20), fill("OLD", "sell", 1, "12", 60 * 24 * 10)]
        self.alpaca.get.return_value = self.alpaca.get.return_value + old
        self.assertEqual(self.get(period="1W").context["data"]["stats"]["count"], 3)
        cache.clear()
        self.assertEqual(self.get(period="1M").context["data"]["stats"]["count"], 4)

    def test_responses_are_cached_for_60_seconds(self, _outlook):
        self.get()
        self.get()
        self.assertEqual(self.alpaca.get_account.call_count, 1)
        self.assertEqual(self.alpaca.get_orders.call_count, 1)
        self.assertEqual(grok_bot.CACHE_SECONDS, 60)

    def test_view_never_calls_an_order_method(self, _outlook):
        self.set_prefix("grok-")
        for params in ({}, {"scope": "account"}, {"period": "all"}):
            cache.clear()
            self.get(**params)
        self.client.post(reverse("grok_bot"), {"order_id_prefix": "grok-"})
        for name in WRITE_METHODS:
            getattr(self.alpaca, name).assert_not_called()
        called = {c[0].split(".")[0] for c in self.alpaca.method_calls}
        self.assertTrue(called <= READ_METHODS, called)
        get_paths = {c.args[0] for c in self.alpaca.get.call_args_list}
        self.assertEqual(get_paths, {"/account/activities/FILL"})

    def test_read_only_wrapper_exposes_no_order_methods(self, _outlook):
        wrapper = ReadOnlyAlpaca("k", "s")
        public = {n for n in dir(wrapper) if not n.startswith("_")}
        self.assertEqual(public, {"account", "positions", "orders", "portfolio_history", "fill_activities",
                                  "FILL_ACTIVITIES_PATH"})
        source = inspect.getsource(grok_bot)
        for name in WRITE_METHODS[:7]:
            self.assertNotRegex(source, rf"\.{name}\(")

    def test_api_errors_show_a_message_and_keep_the_rest(self, _outlook):
        self.alpaca.get_account.side_effect = RuntimeError("boom")
        self.alpaca.get_orders.side_effect = RuntimeError("boom")
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Couldn&#x27;t load account from Alpaca right now.")
        self.assertContains(response, "Couldn&#x27;t load orders from Alpaca right now.")
        self.assertContains(response, "Orders couldn't be loaded.")
        self.assertContains(response, describe_contract(CALL))  # positions still shown
        self.assertNotContains(response, "boom")

    def test_everything_failing_still_renders(self, _outlook):
        for name in READ_METHODS:
            getattr(self.alpaca, name).side_effect = ConnectionError("down")
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["data"]["errors"]), 5)
        self.assertContains(response, "No equity history for this period.")

    def test_missing_credentials(self, _outlook):
        AlpacaCredentials.objects.filter(user=self.user).delete()
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'id="grok-not-connected"')
        self.assertContains(response, reverse("connect_alpaca_account"))
        self.client_class.assert_not_called()

    def test_unreadable_credentials(self, _outlook):
        AlpacaCredentials.objects.filter(user=self.user).update(encrypted_api_key="not-a-fernet-token")
        response = self.get()
        self.assertContains(response, "Couldn&#x27;t read your saved Alpaca credentials")
        self.client_class.assert_not_called()

    def test_live_account_is_never_read(self, _outlook):
        AlpacaCredentials.objects.filter(user=self.user).update(is_paper=False)
        response = self.get()
        self.assertContains(response, "only reads paper accounts")
        self.client_class.assert_not_called()

    def test_login_required(self, _outlook):
        self.client.logout()
        self.assertEqual(self.get().status_code, 302)

    def test_nav_item_in_the_trading_app_only(self, _outlook):
        link = f'<a href="{reverse("grok_bot")}">Grok Bot</a>'
        self.assertContains(self.client.get(reverse("trading_home")), link)
        self.assertContains(self.get(), link)
        self.assertNotContains(self.client.get(reverse("albright_reselling_app:dashboard")), link)
        self.assertNotContains(self.client.get(reverse("albright_crypto_app:dashboard")), link)
