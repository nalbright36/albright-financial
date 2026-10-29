"""Grok Bot multi-strategy tracking: several bot strategies in one Alpaca
paper account, told apart by client_order_id prefix (strategy_attribution.py).
Alpaca is mocked at the TradingClient class - no test reaches the real API.

Covers prefix matching (any capitalization, several prefixes, the longest
wins) and the ambiguous-prefix warnings; multi-leg orders attributed by their
parent; FIFO realized P&L per strategy with the x100 options multiplier; two
strategies holding the same contract keeping separate quantities, cost and
P&L ("shared contract"); the Unattributed bucket; win rate, profit factor and
max drawdown; options closed at $0 after expiring; the snapshot command and
its backfill; the migration of the old "eng" setting into a strategy; bot
notes tagged with a strategy and GET trading/strategies; the Strategies
manager; and read-only (no order method is ever called)."""
import importlib
import json
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotRequestLog, BotToken, TradingNote
from albright_trading_app import strategy_attribution as sa
from albright_trading_app.grok_bot import normalize_fill
from albright_trading_app.models import AlpacaCredentials, BotStrategy, StrategyDailySnapshot
from albright_trading_app.test_grok_bot import (CALL, HOME_OUTLOOK, LONG_PUT, NOW, PUT, READ_METHODS, SHORT_PUT,
                                                TRADING_CLIENT, WRITE_METHODS, eng_client, fake_client, fill, order)

ENG = sa.StrategyRef("1", "eng", ("eng",), "#3B82F6")
INTRA = sa.StrategyRef("2", "intra", ("intra", "INTRA2"), "#F97316", capital=Decimal("1000"))
EXPIRED_CALL = "SPY" + (timezone.localdate() - timedelta(days=5)).strftime("%y%m%d") + "C00500000"


def fills(*raw):
    return [normalize_fill(f) for f in raw]


def noon(days_ago):
    """Noon (local) `days_ago` days back - so "which day" never depends on when the test runs."""
    day = timezone.localdate() - timedelta(days=days_ago)
    return timezone.make_aware(timezone.datetime.combine(day, timezone.datetime.min.time())) + timedelta(hours=12)


def fill_at(symbol, side, qty, price, when, order_id, fill_id=None):
    return {"id": fill_id or f"{symbol}-{side}-{when.isoformat()}-{order_id}", "activity_type": "FILL", "symbol": symbol,
            "side": side, "qty": str(qty), "price": str(price), "order_id": order_id,
            "transaction_time": when.isoformat()}


def account_position(symbol, qty, current, side="long", lastday=None):
    return {"symbol": symbol, "qty": Decimal(qty), "side": side, "current_price": Decimal(current),
            "lastday_price": Decimal(lastday) if lastday else None}


# ---------------------------------------------------------------------------
# Prefixes and attribution (pure)
# ---------------------------------------------------------------------------

class PrefixTests(TestCase):
    def test_case_insensitive_and_several_prefixes(self):
        refs = [ENG, INTRA]
        for cid, key in [("eng-abc-open-1", "1"), ("ENG-test-close-2", "1"), ("Eng", "1"), ("intra-x-o1", "2"),
                         ("INTRA-x", "2"), ("intra2-y", "2"), ("x-eng-1", sa.UNATTRIBUTED), ("", sa.UNATTRIBUTED),
                         (None, sa.UNATTRIBUTED), ("631380d2-5b41-4f9c", sa.UNATTRIBUTED)]:
            with self.subTest(cid=cid):
                self.assertEqual(sa.match_strategy(cid, refs), key)

    def test_the_longest_matching_prefix_wins(self):
        short, long = sa.StrategyRef("1", "Short", ("eng",)), sa.StrategyRef("2", "Long", ("eng-v2",))
        self.assertEqual(sa.match_strategy("eng-v2-open-1", [short, long]), "2")
        self.assertEqual(sa.match_strategy("eng-v1-open-1", [short, long]), "1")

    def test_ambiguous_prefix_warnings(self):
        self.assertEqual(sa.ambiguities([ENG, INTRA]), [])
        shared = sa.ambiguities([ENG, sa.StrategyRef("3", "copy", ("ENG",))])
        self.assertEqual(len(shared), 1)
        self.assertIn('"eng" and "copy" both use the prefix "eng"', shared[0])
        nested = sa.ambiguities([ENG, sa.StrategyRef("3", "v2", ("eng-v2",))])
        self.assertEqual(len(nested), 1)
        self.assertIn('"eng-v2" (v2) starts with "eng" (eng)', nested[0])
        self.assertIn("it goes to v2, the longer prefix", nested[0])
        # Two prefixes of ONE strategy can overlap harmlessly.
        self.assertEqual(sa.ambiguities([sa.StrategyRef("1", "eng", ("eng", "eng-test"))]), [])

    def test_parse_prefixes(self):
        self.assertEqual(sa.parse_prefixes("eng, ENG  intra,,eng-v2"), ["eng", "intra", "eng-v2"])
        self.assertEqual(sa.parse_prefixes("  "), [])

    def test_multi_leg_orders_are_attributed_by_their_parent(self):
        from albright_trading_app.grok_bot import order_ids_to_client_ids
        client = eng_client()
        id_to_key = sa.order_strategies(order_ids_to_client_ids(client.get_orders()), [ENG])
        for leg_id in ("l-c1", "l-c2", "l-o1", "l-o2", "l-x1"):       # legs carry random uuids of their own
            self.assertEqual(id_to_key[leg_id], "1", leg_id)
        self.assertEqual((id_to_key["m-nke"], id_to_key["m-eng-look"]), (sa.UNATTRIBUTED, sa.UNATTRIBUTED))
        buckets = sa.split_fills(fills(*client.get()), id_to_key)
        self.assertEqual({f["symbol"] for f in buckets["1"]}, {SHORT_PUT, LONG_PUT})
        # NKE's opening fill is by an order not in the history: it can't be attributed.
        self.assertEqual([f["order_id"] for f in buckets[sa.UNATTRIBUTED]], ["m-nke-old", "m-nke"])


# ---------------------------------------------------------------------------
# FIFO and the numbers per strategy (pure)
# ---------------------------------------------------------------------------

class StrategyPnlTests(TestCase):
    def setUp(self):
        # eng buys 2 calls @2.00 and sells 1 @3.50; intra buys 1 @3.00 - both in the same contract.
        self.raw = [fill(CALL, "buy", 2, "2.00", 300, "eng-1"), fill(CALL, "buy", 1, "3.00", 200, "intra-1"),
                    fill(CALL, "sell", 1, "3.50", 100, "eng-2"), fill("AAPL", "buy", 10, "190", 400, "manual"),
                    fill("AAPL", "sell", 10, "200", 50, "manual")]
        self.id_to_key = {"eng-1": "1", "eng-2": "1", "intra-1": "2"}
        self.positions = {CALL: account_position(CALL, 2, "4.00", lastday="3.80")}

    def reports(self, start=None):
        buckets = sa.split_fills(fills(*self.raw), self.id_to_key)
        rows = [sa.bucket_report(ref, buckets.get(ref.key, []), self.positions, start=start)
                for ref in (ENG, INTRA, sa.UNATTRIBUTED_REF)]
        sa.mark_shared(rows)
        return rows

    def test_fifo_realized_pnl_with_the_options_multiplier(self):
        eng, intra, other = self.reports()
        self.assertEqual(eng["realized_to_date"], Decimal("150.00"))      # (3.50 - 2.00) x 1 x 100
        self.assertEqual(intra["realized_to_date"], Decimal("0.00"))
        self.assertEqual(other["realized_to_date"], Decimal("100.00"))    # stock: no multiplier
        self.assertEqual([t.pnl for t in other["trades"]], [Decimal("100")])

    def test_two_strategies_in_one_contract_keep_their_own_quantity_and_cost(self):
        eng, intra, _other = self.reports()
        [eng_pos], [intra_pos] = eng["open_positions"], intra["open_positions"]
        self.assertEqual((eng_pos["qty"], eng_pos["avg_price"], eng_pos["unrealized_pl"]),
                         (Decimal("1"), Decimal("2.0000"), Decimal("200.00")))       # (4.00 - 2.00) x 100
        self.assertEqual((intra_pos["qty"], intra_pos["avg_price"], intra_pos["unrealized_pl"]),
                         (Decimal("1"), Decimal("3.0000"), Decimal("100.00")))       # (4.00 - 3.00) x 100
        self.assertEqual((eng_pos["shared_with"], intra_pos["shared_with"]), (["intra"], ["eng"]))
        self.assertEqual((eng["total_pl"], intra["total_pl"]), (Decimal("350.00"), Decimal("100.00")))
        self.assertEqual(intra["return_pct"], Decimal("10.00"))                       # 100 / 1,000 allocated
        self.assertIsNone(eng["return_pct"])                                          # no capital allocated

    def test_today_is_realized_today_plus_the_intraday_change(self):
        self.raw = [fill_at(CALL, "buy", 1, "2.00", noon(3), "eng-1"), fill_at(CALL, "buy", 1, "3.00", NOW, "eng-2")]
        self.id_to_key = {"eng-1": "1", "eng-2": "1"}
        eng = self.reports()[0]
        # old lot from yesterday's close 3.80 -> 4.00 = +20; today's lot from its cost 3.00 -> 4.00 = +100
        self.assertEqual(eng["today_pl"], Decimal("120.00"))

    def test_unattributed_bucket_and_total(self):
        eng, intra, other = self.reports()
        total = sa.total_report([eng, intra, other])
        self.assertEqual(other["name"], "Unattributed (manual/other)")
        self.assertEqual(total["total_pl"], Decimal("550.00"))
        self.assertEqual(total["stats"]["count"], 1)            # eng's partial exit isn't a closed trade yet
        self.assertEqual(total["open_count"], 1)                # one contract, held by two strategies

    def test_expired_options_close_at_zero(self):
        ledger = sa.ledger_from(fills(fill(EXPIRED_CALL, "buy", 2, "1.50", 60 * 24 * 10, "eng-1")),
                                as_of=timezone.localdate())
        [trade] = ledger.trades
        self.assertEqual((trade.pnl, trade.expired), (Decimal("-300.00"), True))
        self.assertEqual(ledger.open_positions(), {})
        still_held = sa.ledger_from(fills(fill(EXPIRED_CALL, "buy", 2, "1.50", 60 * 24 * 10, "eng-1")),
                                    as_of=timezone.localdate(), held={EXPIRED_CALL})
        self.assertEqual(still_held.trades, [])                 # the account still shows it: not expired yet

    def test_fees_count_against_the_strategy(self):
        raw = [dict(fill(CALL, "buy", 1, "2.00", 30, "eng-1"), fee="0.65"),
               dict(fill(CALL, "sell", 1, "3.00", 20, "eng-1"), fee="0.65")]
        row = sa.bucket_report(ENG, fills(*raw), {})
        self.assertEqual(row["realized_to_date"], Decimal("98.70"))


class StatisticsTests(TestCase):
    def trade(self, pnl):
        return SimpleNamespace(pnl=Decimal(pnl))

    def test_profit_factor(self):
        self.assertEqual(sa.profit_factor([self.trade(5), self.trade(-3), self.trade(1), self.trade(0)]), Decimal("2.00"))
        self.assertIsNone(sa.profit_factor([self.trade(5)]))
        self.assertIsNone(sa.profit_factor([]))

    def test_max_drawdown(self):
        values = [0, 100, 50, 120, 20, 60]
        self.assertEqual(sa.max_drawdown(values), (Decimal("100.00"), None))            # 120 -> 20
        amount, pct = sa.max_drawdown(values, capital=Decimal("1000"))
        self.assertEqual((amount, pct), (Decimal("100.00"), Decimal("8.93")))            # 100 / (1,000 + 120)
        self.assertEqual(sa.max_drawdown([0, 10, 20]), (Decimal("0.00"), None))
        self.assertEqual(sa.max_drawdown([0, -50], capital=500), (Decimal("50.00"), Decimal("10.00")))

    def test_win_rate_and_averages_per_strategy(self):
        raw = [fill("A", "buy", 1, "10", 90, "eng-1"), fill("A", "sell", 1, "15", 80, "eng-1"),
               fill("B", "buy", 1, "10", 70, "eng-1"), fill("B", "sell", 1, "7", 60, "eng-1"),
               fill("C", "buy", 1, "10", 50, "eng-1"), fill("C", "sell", 1, "11", 40, "eng-1")]
        row = sa.bucket_report(ENG, fills(*raw), {})
        self.assertEqual((row["stats"]["win_rate"], row["stats"]["avg_win"], row["stats"]["avg_loss"],
                          row["stats"]["profit_factor"], row["stats"]["count"]),
                         (Decimal("66.7"), Decimal("3.00"), Decimal("-3.00"), Decimal("2.00"), 3))
        self.assertEqual(row["max_drawdown"], Decimal("3.00"))                          # +5 then -3

    def test_daily_history_and_cumulative_series(self):
        raw = fills(fill_at(CALL, "buy", 1, "2.00", noon(3), "x"), fill_at(CALL, "sell", 1, "3.00", noon(2), "x"),
                    fill_at(PUT, "sell", 1, "1.00", noon(1), "x"))
        today = timezone.localdate()
        history = sa.daily_history(raw, today - timedelta(days=1))
        self.assertEqual(history, [(today - timedelta(days=3), Decimal("0.00"), 1, 0),
                                   (today - timedelta(days=2), Decimal("100.00"), 0, 1),
                                   (today - timedelta(days=1), Decimal("100.00"), 1, 0)])
        ledger = sa.ledger_from(raw)
        snapshots = {today - timedelta(days=1): Decimal("130")}        # a snapshot with unrealized P&L
        series = sa.cumulative_series(ledger.realizations, today - timedelta(days=2), today, Decimal("150"), snapshots,
                                      baseline_day=today - timedelta(days=3))
        self.assertEqual([v for _d, v in series], [Decimal("100.00"), Decimal("130.00"), Decimal("150.00")])


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

def two_strategy_client():
    """The account two strategies (eng, intra) and a person trade in, with a contract eng and intra both hold."""
    client = fake_client()
    client.get_orders.return_value = [
        order("e1", "eng-abc-open-1-x", symbol=CALL), order("e2", "ENG-abc-close-1-x", symbol=CALL, side="sell"),
        order("i1", "intra-abc-o1-x", symbol=CALL),
        order("e3", "eng-def-open-2-x", symbol=None, side=None, legs=[
            SimpleNamespace(id="leg-1", symbol=PUT, side="sell", ratio_qty="1", client_order_id="uuid-1"),
            SimpleNamespace(id="leg-2", symbol=LONG_PUT, side="buy", ratio_qty="1", client_order_id="uuid-2")]),
        order("m1", "631380d2-5b41-4f9c-9e86-90d2a26efc6d", symbol="AAPL"),
    ]
    client.get.return_value = [
        fill(CALL, "buy", 2, "2.00", 300, "e1"), fill(CALL, "buy", 1, "3.00", 200, "i1"),
        fill(CALL, "sell", 1, "3.50", 100, "e2"),
        fill(PUT, "sell", 1, "1.50", 90, "leg-1"), fill(PUT, "buy", 1, "1.00", 80, "leg-1"),
        fill("AAPL", "buy", 10, "190", 400, "m1"),
    ]
    client.get_all_positions.return_value = [
        SimpleNamespace(symbol=CALL, qty="2", side="long", avg_entry_price="2.50", current_price="4.00",
                        market_value="800", unrealized_pl="300", unrealized_plpc="0.6", lastday_price="3.80"),
        SimpleNamespace(symbol="AAPL", qty="10", side="long", avg_entry_price="190", current_price="195",
                        market_value="1950", unrealized_pl="50", unrealized_plpc="0.026"),
    ]
    return client


class PageBase(TestCase):
    def setUp(self):
        cache.clear()
        outlook = mock.patch(HOME_OUTLOOK, return_value=None)
        outlook.start()
        self.addCleanup(outlook.stop)
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.force_login(self.user)
        creds = AlpacaCredentials(user=self.user, is_paper=True)
        creds.set_credentials("PKTEST", "secret-test")
        creds.save()
        self.alpaca = two_strategy_client()
        patcher = mock.patch(TRADING_CLIENT, return_value=self.alpaca)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.eng = BotStrategy.objects.create(user=self.user, name="eng", prefixes=["eng"], color="#3B82F6",
                                              display_order=0)
        self.intra = BotStrategy.objects.create(user=self.user, name="Intraday", prefixes=["intra"], color="#F97316",
                                                display_order=1, allocated_capital=Decimal("2000"))


class StrategyPageTests(PageBase):
    def test_comparison_table(self):
        response = self.client.get(reverse("grok_bot"))
        report = response.context["data"]["report"]
        eng, intra = report["rows"]
        self.assertEqual((eng["name"], intra["name"]), ("eng", "Intraday"))
        self.assertEqual(eng["realized_to_date"], Decimal("200.00"))         # call +150, put spread +50
        self.assertEqual(intra["total_pl"], Decimal("100.00"))
        self.assertEqual(intra["return_pct"], Decimal("5.00"))
        self.assertEqual(report["unattributed"]["open_count"], 1)            # AAPL
        content = response.content.decode()
        for key in ("today", "total", "return", "winrate", "avgwin", "avgloss", "pf", "trades", "dd", "open", "status"):
            self.assertIn(f'data-sort-key="{key}"', content)
        self.assertIn(f'href="{reverse("grok_bot_strategy", args=[self.eng.pk])}?period=1M"', content)
        self.assertIn(f'href="{reverse("grok_bot_strategy", args=["unattributed"])}?period=1M"', content)
        self.assertIn("<strong>Total</strong>", content)

    def test_positions_show_who_holds_them_and_shared_contracts(self):
        response = self.client.get(reverse("grok_bot"))
        call = next(p for p in response.context["data"]["positions"] if p["symbol"] == CALL)
        self.assertEqual([(h["name"], h["qty"]) for h in call["holders"]], [("eng", 1), ("Intraday", 1)])
        self.assertTrue(call["shared"])
        self.assertEqual(call["strategies"], f"{self.eng.pk} {self.intra.pk}")
        self.assertContains(response, "shared contract - each strategy keeps its own quantity and cost")

    def test_filter_and_tabs_carry_the_strategy(self):
        response = self.client.get(reverse("grok_bot"), {"strategy": str(self.intra.pk)})
        self.assertContains(response, f'<option value="{self.intra.pk}" selected>Intraday</option>', html=True)
        self.assertContains(response, '<option value="unattributed">Unattributed (manual/other)</option>', html=True)
        content = response.content.decode()
        self.assertIn(f'<tr data-strategies="{self.eng.pk}">', content)          # orders and trades rows
        self.assertIn('<tr data-strategies="unattributed">', content)

    def test_chart_series_per_strategy_in_its_color(self):
        response = self.client.get(reverse("grok_bot"))
        series = {s["name"]: s for s in response.context["series"]}
        self.assertEqual(series["eng"]["color"], "#3B82F6")
        self.assertEqual(series["Intraday"]["points"][-1]["value"], 100.0)
        self.assertEqual(series["eng"]["points"][-1]["value"], 400.0)               # 200 realized + 200 unrealized
        self.assertIn('<script id="grok-series" type="application/json">', response.content.decode())
        json.loads(response.content.decode().split('<script id="grok-series" type="application/json">')[1]
                   .split("</script>")[0])

    def test_ambiguous_prefix_warning_on_the_page(self):
        BotStrategy.objects.create(user=self.user, name="eng v2", prefixes=["eng-v2"], display_order=2)
        response = self.client.get(reverse("grok_bot"))
        self.assertContains(response, "starts with &quot;eng&quot; (eng)")

    def test_detail_page(self):
        response = self.client.get(reverse("grok_bot_strategy", args=[self.eng.pk]))
        self.assertEqual(response.status_code, 200)
        row = response.context["row"]
        self.assertEqual([p["symbol"] for p in row["open_positions"]], [CALL])
        self.assertEqual([o["client_order_id"] for o in response.context["orders"]],
                         ["eng-abc-open-1-x", "ENG-abc-close-1-x", "eng-def-open-2-x"])
        self.assertContains(response, "shared contract - also held by Intraday")
        self.assertContains(response, 'id="tile-drawdown"')
        self.assertContains(response, '<code>eng</code>')
        unattributed = self.client.get(reverse("grok_bot_strategy", args=["unattributed"]))
        self.assertEqual([o["client_order_id"] for o in unattributed.context["orders"]],
                         ["631380d2-5b41-4f9c-9e86-90d2a26efc6d"])
        self.assertContains(unattributed, "Unattributed (manual/other)")

    def test_detail_page_is_only_for_your_strategies(self):
        other = User.objects.create_user(username="other", password="pw-not-real-12345")
        theirs = BotStrategy.objects.create(user=other, name="theirs", prefixes=["t"])
        self.assertEqual(self.client.get(reverse("grok_bot_strategy", args=[theirs.pk])).status_code, 404)
        self.assertEqual(self.client.get(reverse("grok_bot_strategy", args=["nope"])).status_code, 404)

    def test_notes_tagged_with_a_strategy_show_on_its_page(self):
        TradingNote.objects.create(owner=self.user, date=timezone.localdate(), title="Eng note", text="x", strategy=self.eng)
        TradingNote.objects.create(owner=self.user, date=timezone.localdate(), title="Untagged note", text="y")
        eng_page = self.client.get(reverse("grok_bot_strategy", args=[self.eng.pk]))
        self.assertContains(eng_page, "Eng note")
        self.assertNotContains(eng_page, "Untagged note")
        self.assertContains(self.client.get(reverse("grok_bot_strategy", args=["unattributed"])), "Untagged note")
        intra_page = self.client.get(reverse("grok_bot_strategy", args=[self.intra.pk]))
        self.assertContains(intra_page, "No notes tagged with this strategy yet.")
        main = self.client.get(reverse("grok_bot"))
        self.assertContains(main, "Eng note")
        self.assertContains(main, "Untagged note")


class StrategyManagerTests(PageBase):
    def test_add(self):
        response = self.client.post(reverse("grok_bot_strategy_add"), {
            "name": "Momentum", "prefixes_text": "mom, MOM2", "color": "#10B981", "status": "active",
            "started_at": "2026-10-01", "allocated_capital": "5000", "notes": "Breakouts."})
        self.assertRedirects(response, reverse("grok_bot") + "#strategies", fetch_redirect_response=False)
        new = BotStrategy.objects.get(name="Momentum")
        self.assertEqual((new.prefixes, new.display_order, new.allocated_capital), (["mom", "MOM2"], 2, Decimal("5000")))

    def test_add_validation(self):
        for data, error in [({"name": "ENG", "prefixes_text": "x"}, "You already have a strategy with this name."),
                            ({"name": "New", "prefixes_text": " "}, "This field is required."),
                            ({"name": "New", "prefixes_text": "bad prefix<"}, "Use letters, numbers")]:
            with self.subTest(data=data):
                response = self.client.post(reverse("grok_bot_strategy_add"),
                                            {"color": "#10B981", "status": "active", **data})
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, error, status_code=400)
        self.assertEqual(BotStrategy.objects.count(), 2)

    def test_edit_rename(self):
        response = self.client.post(reverse("grok_bot_strategy_edit", args=[self.eng.pk]), {
            "name": "Engulfing", "prefixes_text": "eng, eng-test", "color": "#A855F7", "status": "active"})
        self.assertRedirects(response, reverse("grok_bot") + "#strategies", fetch_redirect_response=False)
        self.eng.refresh_from_db()
        self.assertEqual((self.eng.name, self.eng.prefixes, self.eng.color), ("Engulfing", ["eng", "eng-test"], "#A855F7"))
        page = self.client.get(reverse("grok_bot_strategy_edit", args=[self.eng.pk]))
        self.assertContains(page, 'value="eng, eng-test"')

    def test_pause_retire_and_reactivate(self):
        url = reverse("grok_bot_strategy_status", args=[self.eng.pk])
        for status in ("paused", "retired", "active"):
            self.client.post(url, {"status": status})
            self.eng.refresh_from_db()
            self.assertEqual(self.eng.status, status)
        self.client.post(url, {"status": "deleted"})
        self.eng.refresh_from_db()
        self.assertEqual(self.eng.status, "active")
        self.assertEqual(self.client.get(url).status_code, 405)

    def test_retired_strategies_keep_their_orders(self):
        self.eng.status = "retired"
        self.eng.save()
        row = self.client.get(reverse("grok_bot")).context["data"]["report"]["rows"][0]
        self.assertEqual((row["status"], row["realized_to_date"]), ("retired", Decimal("200.00")))

    def test_reorder(self):
        self.client.post(reverse("grok_bot_strategy_move", args=[self.intra.pk]), {"direction": "up"})
        self.assertEqual([s.name for s in BotStrategy.objects.filter(user=self.user)], ["Intraday", "eng"])
        self.client.post(reverse("grok_bot_strategy_move", args=[self.intra.pk]), {"direction": "up"})   # already first
        self.assertEqual([s.name for s in BotStrategy.objects.filter(user=self.user)], ["Intraday", "eng"])
        self.client.post(reverse("grok_bot_strategy_move", args=[self.intra.pk]), {"direction": "down"})
        self.assertEqual([s.name for s in BotStrategy.objects.filter(user=self.user)], ["eng", "Intraday"])

    def test_cannot_touch_someone_elses(self):
        other = User.objects.create_user(username="other", password="pw-not-real-12345")
        theirs = BotStrategy.objects.create(user=other, name="theirs", prefixes=["t"])
        for name in ("grok_bot_strategy_edit", "grok_bot_strategy_status", "grok_bot_strategy_move"):
            self.assertEqual(self.client.post(reverse(name, args=[theirs.pk]), {"status": "retired"}).status_code, 404)
        theirs.refresh_from_db()
        self.assertEqual(theirs.status, "active")


# ---------------------------------------------------------------------------
# Daily snapshots
# ---------------------------------------------------------------------------

class SnapshotCommandTests(PageBase):
    def setUp(self):
        super().setUp()
        self.alpaca.get.return_value = [
            fill_at(CALL, "buy", 1, "2.00", noon(3), "e1"), fill_at(CALL, "sell", 1, "3.00", noon(2), "e2"),
            fill_at(CALL, "buy", 1, "3.00", noon(1), "i1"),
            fill_at("AAPL", "buy", 10, "190", noon(1), "m1"),
        ]
        self.today = timezone.localdate()

    def run_command(self, *args):
        out = StringIO()
        call_command("snapshot_strategies", *args, stdout=out)
        return out.getvalue()

    def test_first_run_backfills_then_snapshots_today(self):
        out = self.run_command()
        eng = list(StrategyDailySnapshot.objects.filter(strategy=self.eng).values_list(
            "date", "realized_pnl", "unrealized_pnl", "open_positions", "trades_closed", "backfilled"))
        d = lambda n: self.today - timedelta(days=n)  # noqa: E731
        self.assertEqual(eng, [(d(3), Decimal("0.00"), None, 1, 0, True), (d(2), Decimal("100.00"), None, 0, 1, True),
                               (d(1), Decimal("100.00"), None, 0, 0, True),
                               (d(0), Decimal("100.00"), Decimal("0.00"), 0, 0, False)])
        intra_today = StrategyDailySnapshot.objects.get(strategy=self.intra, date=self.today)
        self.assertEqual((intra_today.realized_pnl, intra_today.unrealized_pnl, intra_today.open_positions),
                         (Decimal("0.00"), Decimal("100.00"), 1))                 # (4.00 - 3.00) x 100
        unattributed = StrategyDailySnapshot.objects.get(user=self.user, strategy=None, date=self.today)
        self.assertEqual(unattributed.unrealized_pnl, Decimal("50.00"))           # AAPL (195 - 190) x 10
        self.eng.refresh_from_db()
        self.assertEqual(self.eng.started_at, d(3))
        self.assertIn("eng: realized $100.00", out)
        self.assertIn("backfilled 3 days", out)

    def test_running_again_only_updates_today(self):
        self.run_command()
        count = StrategyDailySnapshot.objects.count()
        out = self.run_command()
        self.assertEqual(StrategyDailySnapshot.objects.count(), count)
        self.assertNotIn("backfilled", out)

    def test_rebuild_redoes_the_history(self):
        self.run_command()
        StrategyDailySnapshot.objects.filter(strategy=self.eng, backfilled=True).update(realized_pnl=Decimal("999"))
        self.run_command("--rebuild")
        self.assertEqual(StrategyDailySnapshot.objects.get(strategy=self.eng, date=self.today - timedelta(days=2))
                         .realized_pnl, Decimal("100.00"))

    def test_snapshots_feed_the_chart(self):
        self.run_command()
        StrategyDailySnapshot.objects.filter(strategy=self.intra, date=self.today - timedelta(days=1)) \
            .update(unrealized_pnl=Decimal("40"))
        StrategyDailySnapshot.objects.filter(strategy=self.intra, date=self.today - timedelta(days=1)).update(backfilled=False)
        series = {s["name"]: s for s in self.client.get(reverse("grok_bot"), {"period": "1W"}).context["series"]}
        values = [p["value"] for p in series["Intraday"]["points"]]
        self.assertEqual(values[-2:], [40.0, 100.0])          # yesterday's snapshot, then today live

    def test_live_accounts_and_read_only(self):
        AlpacaCredentials.objects.filter(user=self.user).update(is_paper=False)
        self.assertIn("never read", self.run_command())
        self.assertEqual(StrategyDailySnapshot.objects.count(), 0)
        AlpacaCredentials.objects.filter(user=self.user).update(is_paper=True)
        self.run_command()
        for name in WRITE_METHODS:
            getattr(self.alpaca, name).assert_not_called()
        self.assertTrue({c[0].split(".")[0] for c in self.alpaca.method_calls} <= READ_METHODS)


# ---------------------------------------------------------------------------
# Bot API: notes tagged with a strategy, GET trading/strategies
# ---------------------------------------------------------------------------

@override_settings(BOT_API_REQUIRE_HTTPS=False)
class BotApiStrategyTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username="nate", password="pw-not-real-12345")
        self.eng = BotStrategy.objects.create(user=self.owner, name="Engulfing", prefixes=["eng"], display_order=0)
        BotStrategy.objects.create(user=self.owner, name="Intraday", prefixes=["intra", "INTRA2"], status="paused",
                                   display_order=1)
        _token, self.raw = BotToken.issue(self.owner, "Grok", ["trading:notes"])
        _token, self.other_raw = BotToken.issue(self.owner, "Reader", ["reseller:read"])

    def post(self, body, raw=None):
        return self.client.post("/api/bot/v1/trading/notes", data=json.dumps(body), content_type="application/json",
                                 HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}")

    def test_note_tagged_by_name_or_prefix(self):
        for value in ("Engulfing", "engulfing", "eng", "ENG"):
            with self.subTest(value=value):
                response = self.post({"title": "Closed", "text": "50%", "strategy": value})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["strategy"], "Engulfing")
                self.assertEqual(TradingNote.objects.get(pk=response.json()["id"]).strategy, self.eng)
        untagged = self.post({"title": "Plain", "text": "x"})
        self.assertEqual((untagged.status_code, untagged.json()["strategy"]), (201, None))

    def test_unknown_strategy_is_a_clear_error(self):
        response = self.post({"title": "x", "text": "y", "strategy": "nope"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], 'No strategy "nope". Your strategies: Engulfing, Intraday.')
        self.assertEqual(self.post({"title": "x", "text": "y", "strategy": 5}).status_code, 400)
        self.assertFalse(TradingNote.objects.exists())

    def test_list_strategies(self):
        response = self.client.get("/api/bot/v1/trading/strategies", HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(response.json(), {"strategies": [
            {"name": "Engulfing", "prefixes": ["eng"], "status": "active"},
            {"name": "Intraday", "prefixes": ["intra", "INTRA2"], "status": "paused"}]})
        self.assertTrue(BotRequestLog.objects.filter(endpoint="/api/bot/v1/trading/strategies", status=200).exists())
        denied = self.client.get("/api/bot/v1/trading/strategies", HTTP_AUTHORIZATION=f"Bearer {self.other_raw}")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.client.get("/api/bot/v1/trading/strategies").status_code, 401)

    def test_documented(self):
        docs = self.client.get("/api/bot/v1/docs", HTTP_AUTHORIZATION=f"Bearer {self.raw}").content.decode()
        self.assertIn("GET  trading/strategies", docs)
        self.assertIn('"strategy": "eng"}', docs)


# ---------------------------------------------------------------------------
# The migration: the old single "Bot order ID prefix" becomes a strategy
# ---------------------------------------------------------------------------

class _OldSettings:
    """Stands in for the removed GrokBotSettings table, so the migration's functions run in an ordinary
    (rolled-back) test instead of migrating the shared test database back and forth."""
    def __init__(self, rows):
        self.rows = [SimpleNamespace(**row) for row in rows]
        self.objects = self

    def exclude(self, order_id_prefix):
        return [row for row in self.rows if row.order_id_prefix != order_id_prefix]

    def filter(self, user_id):
        return SimpleNamespace(exists=lambda: any(row.user_id == user_id for row in self.rows))

    def create(self, **fields):
        self.rows.append(SimpleNamespace(**fields))


class PrefixMigrationTests(TestCase):
    migration = importlib.import_module("albright_trading_app.migrations.0019_bot_strategies")

    def apps(self, old_settings):
        models = {"GrokBotSettings": old_settings, "BotStrategy": BotStrategy}
        return SimpleNamespace(get_model=lambda app, name: models[name])

    def test_eng_prefix_becomes_a_strategy_named_eng(self):
        user = User.objects.create_user("nate", password="x")
        blank = User.objects.create_user("sam", password="x")
        old = _OldSettings([{"user_id": user.pk, "order_id_prefix": "eng"}, {"user_id": blank.pk, "order_id_prefix": ""}])
        self.migration.prefix_to_strategy(self.apps(old), None)
        strategies = list(BotStrategy.objects.values("user_id", "name", "prefixes", "status", "display_order"))
        self.assertEqual(strategies, [{"user_id": user.pk, "name": "eng", "prefixes": ["eng"], "status": "active",
                                       "display_order": 0}])

    def test_existing_strategies_are_left_alone(self):
        user = User.objects.create_user("nate", password="x")
        BotStrategy.objects.create(user=user, name="Mine", prefixes=["mine"])
        self.migration.prefix_to_strategy(self.apps(_OldSettings([{"user_id": user.pk, "order_id_prefix": "eng"}])), None)
        self.assertEqual(list(BotStrategy.objects.values_list("name", flat=True)), ["Mine"])

    def test_going_back_restores_the_first_prefix(self):
        user = User.objects.create_user("nate", password="x")
        BotStrategy.objects.create(user=user, name="eng", prefixes=["eng", "eng2"], display_order=0)
        BotStrategy.objects.create(user=user, name="intra", prefixes=["intra"], display_order=1)
        old = _OldSettings([])
        self.migration.strategy_to_prefix(self.apps(old), None)
        self.assertEqual([(row.user_id, row.order_id_prefix) for row in old.rows], [(user.pk, "eng")])
