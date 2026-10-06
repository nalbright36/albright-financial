"""Tests for scanner/calculator.py's own math (checked against
scanner/max_bid.py directly for the same inputs), presets, the melt
helper, and prefill from a lot. All pure ORM/function calls - no HTTP to
any external site; spot price lookups are given explicitly or mocked.

This module (and the old multi-item/melt-helper Bid Calculator page it
originally backed) is no longer wired to any URL - the Bid Calculator
page now runs entirely on scanner/simple_calc.py instead (see
calculator_views.py and tests/test_bid_calculator_views.py). The tests
below just keep scanner/calculator.py's own math covered since the
module itself is still here, untouched."""
from decimal import Decimal

from django.test import TestCase

from albright_reselling_app.scanner import calculator
from albright_reselling_app.scanner.max_bid import BuyCosts, SellFees, all_in_cost
from albright_reselling_app.scanner.max_bid import max_bid as compute_max_bid
from albright_reselling_app.scanner.max_bid import projected_profit
from albright_reselling_app.scanner_models import AIReview, CalibrationOverride, LotEvaluation, SourcedLot


class ExactMatchWithMaxBidTests(TestCase):
    """With no calculator-only inputs active (card_fee=0, per_lot_fee=0,
    tax_on_premium=True, other_costs=0), every result must equal what
    max_bid.py's own functions give for the same numbers."""

    def setUp(self):
        self.buy = calculator.BuySideInputs(
            bid=100.0, premium_pct=0.18, tax_pct=0.07, tax_on_premium=True, shipping=10.0,
        )
        self.sell = calculator.SellSideInputs(
            items=[calculator.LineItem("ring", 1, 300.0)], platform_fee_pct=0.1325, fixed_fee=0.40,
            outbound_shipping=5.0, packaging=0.50, target_profit_dollars=10.0, target_profit_pct=0.20,
        )
        self.mb_fees = SellFees(0.1325, 0.40, 5.0, 0.50, 10.0, 0.20)
        self.mb_buy = BuyCosts(0.18, 0.07, 10.0)

    def test_total_cost_matches_all_in_cost(self):
        result = calculator.calculate(self.buy, self.sell)
        self.assertAlmostEqual(result["total_cost"], all_in_cost(100.0, self.mb_buy), places=2)

    def test_max_bid_matches_max_bid_py(self):
        result = calculator.calculate(self.buy, self.sell)
        expected = compute_max_bid(300.0, self.mb_fees, self.mb_buy)
        self.assertAlmostEqual(result["max_bid"], expected, places=2)

    def test_profit_matches_projected_profit(self):
        result = calculator.calculate(self.buy, self.sell)
        expected = projected_profit(100.0, 300.0, self.mb_fees, self.mb_buy)
        self.assertAlmostEqual(result["profit"], expected, places=2)


class TaxOnPremiumToggleTests(TestCase):
    def test_tax_on_premium_true_matches_manual_math(self):
        buy = calculator.BuySideInputs(bid=100.0, premium_pct=0.20, tax_pct=0.10, tax_on_premium=True)
        breakdown = buy.breakdown()
        # tax on (hammer + premium) = (100+20)*0.10 = 12
        self.assertAlmostEqual(breakdown["sales_tax"], 12.0, places=2)

    def test_tax_on_premium_false_taxes_hammer_only(self):
        buy = calculator.BuySideInputs(bid=100.0, premium_pct=0.20, tax_pct=0.10, tax_on_premium=False)
        breakdown = buy.breakdown()
        # tax on hammer only = 100*0.10 = 10, strictly less than the "on" case
        self.assertAlmostEqual(breakdown["sales_tax"], 10.0, places=2)

    def test_toggle_changes_total_cost(self):
        on = calculator.BuySideInputs(bid=100.0, premium_pct=0.20, tax_pct=0.10, tax_on_premium=True)
        off = calculator.BuySideInputs(bid=100.0, premium_pct=0.20, tax_pct=0.10, tax_on_premium=False)
        self.assertGreater(all_in_cost(100.0, on.as_buy_costs()), all_in_cost(100.0, off.as_buy_costs()))


class CardAndPerLotFeeTests(TestCase):
    def test_card_fee_increases_total_cost(self):
        no_fee = calculator.BuySideInputs(bid=100.0, premium_pct=0.18, tax_pct=0.07, card_fee_pct=0.0)
        with_fee = calculator.BuySideInputs(bid=100.0, premium_pct=0.18, tax_pct=0.07, card_fee_pct=0.03)
        self.assertGreater(all_in_cost(100.0, with_fee.as_buy_costs()), all_in_cost(100.0, no_fee.as_buy_costs()))

    def test_card_fee_exact_value(self):
        buy = calculator.BuySideInputs(bid=100.0, premium_pct=0.0, tax_pct=0.0, card_fee_pct=0.03)
        breakdown = buy.breakdown()
        self.assertAlmostEqual(breakdown["card_fee"], 3.0, places=2)

    def test_per_lot_fee_adds_flat_amount(self):
        without = calculator.BuySideInputs(bid=100.0, premium_pct=0.18, tax_pct=0.07)
        with_fee = calculator.BuySideInputs(bid=100.0, premium_pct=0.18, tax_pct=0.07, per_lot_fee=5.0)
        diff = all_in_cost(100.0, with_fee.as_buy_costs()) - all_in_cost(100.0, without.as_buy_costs())
        self.assertAlmostEqual(diff, 5.0, places=2)


class PickupCostTests(TestCase):
    def test_pickup_cost_formula(self):
        buy = calculator.BuySideInputs(
            bid=0, inbound_mode="pickup", pickup_miles=20.0, pickup_rate_per_mile=0.70, pickup_fixed_cost=5.0,
        )
        self.assertAlmostEqual(buy.inbound, 2 * 20.0 * 0.70 + 5.0, places=2)

    def test_shipping_mode_ignores_pickup_fields(self):
        buy = calculator.BuySideInputs(
            bid=0, inbound_mode="shipping", shipping=12.0, pickup_miles=100.0, pickup_rate_per_mile=99.0,
        )
        self.assertAlmostEqual(buy.inbound, 12.0, places=2)


class MultipleItemsTests(TestCase):
    def test_expected_sale_sums_quantity_times_price(self):
        sell = calculator.SellSideInputs(items=[
            calculator.LineItem("a", 2, 10.0), calculator.LineItem("b", 1, 50.0), calculator.LineItem("c", 3, 5.0),
        ])
        self.assertAlmostEqual(sell.expected_sale, 2 * 10.0 + 1 * 50.0 + 3 * 5.0, places=2)

    def test_empty_items_gives_zero_expected_sale_and_zero_max_bid(self):
        buy = calculator.BuySideInputs(bid=50.0, premium_pct=0.1, tax_pct=0.05)
        sell = calculator.SellSideInputs(items=[])
        result = calculator.calculate(buy, sell)
        self.assertEqual(result["expected_sale"], 0.0)
        self.assertEqual(result["max_bid"], 0.0)


class TargetProfitTests(TestCase):
    def test_dollar_target_wins_when_larger(self):
        sell = calculator.SellSideInputs(
            items=[calculator.LineItem("x", 1, 50.0)], target_profit_dollars=20.0, target_profit_pct=0.10,
        )
        # 10% of 50 = 5, vs flat $20 -> $20 wins
        self.assertEqual(sell.as_sell_fees().min_profit, 20.0)

    def test_percent_target_wins_when_larger(self):
        sell = calculator.SellSideInputs(
            items=[calculator.LineItem("x", 1, 500.0)], target_profit_dollars=10.0, target_profit_pct=0.20,
        )
        from albright_reselling_app.scanner.max_bid import target_profit as mb_target_profit
        self.assertEqual(mb_target_profit(sell.expected_sale, sell.as_sell_fees()), 100.0)  # 20% of 500


class OverMaxFlagTests(TestCase):
    def test_entered_bid_above_max_is_flagged(self):
        buy = calculator.BuySideInputs(bid=10000.0, premium_pct=0.18, tax_pct=0.07)
        sell = calculator.SellSideInputs(items=[calculator.LineItem("x", 1, 50.0)])
        result = calculator.calculate(buy, sell)
        self.assertTrue(result["over_max"])

    def test_entered_bid_below_max_is_not_flagged(self):
        buy = calculator.BuySideInputs(bid=1.0, premium_pct=0.18, tax_pct=0.07)
        sell = calculator.SellSideInputs(items=[calculator.LineItem("x", 1, 500.0)])
        result = calculator.calculate(buy, sell)
        self.assertFalse(result["over_max"])


class MeltHelperTests(TestCase):
    def test_14k_gold_in_grams(self):
        result = calculator.melt_value("gold", 14, 10.0, "g", 1.0, spot_price=2000.0)
        expected = (10.0 / 31.1035) * 0.583 * 2000.0
        self.assertAlmostEqual(result["value"], round(expected, 2), places=2)

    def test_925_silver_in_dwt(self):
        result = calculator.melt_value("silver", "0.925", 10.0, "dwt", 1.0, spot_price=30.0)
        grams = 10.0 * 1.555174
        expected = (grams / 31.1035) * 0.925 * 30.0
        self.assertAlmostEqual(result["value"], round(expected, 2), places=2)

    def test_999_fine_silver_in_troy_oz(self):
        result = calculator.melt_value("silver", "0.999", 1.0, "oz", 1.0, spot_price=30.0)
        expected = 1.0 * 0.999 * 30.0
        self.assertAlmostEqual(result["value"], round(expected, 2), places=2)

    def test_payout_pct_applied(self):
        full = calculator.melt_value("gold", 14, 10.0, "g", 1.0, spot_price=2000.0)
        half = calculator.melt_value("gold", 14, 10.0, "g", 0.5, spot_price=2000.0)
        self.assertAlmostEqual(half["value"], full["value"] / 2, places=2)

    def test_no_spot_price_available_returns_none_value(self):
        result = calculator.melt_value("gold", 14, 10.0, "g", 1.0, spot_price=None)
        self.assertIsNone(result["value"])


class PresetTests(TestCase):
    def test_shopgoodwill_source_preset(self):
        preset = calculator.source_preset("shopgoodwill")
        self.assertEqual(preset["premium_pct"], 0.0)
        self.assertEqual(preset["inbound_mode"], "shipping")

    def test_maxsold_source_preset_uses_pickup(self):
        preset = calculator.source_preset("maxsold")
        self.assertEqual(preset["inbound_mode"], "pickup")
        self.assertIn("pickup_rate_per_mile", preset)

    def test_source_preset_honors_calibration_override(self):
        CalibrationOverride.objects.create(key="SOURCES.shopgoodwill.buyer_premium_pct", value=Decimal("0.05"))
        preset = calculator.source_preset("shopgoodwill")
        self.assertEqual(preset["premium_pct"], 0.05)

    def test_scrap_gold_channel_has_no_fees(self):
        preset = calculator.channel_preset("scrap_gold")
        self.assertEqual(preset["platform_fee_pct"], 0.0)
        self.assertEqual(preset["outbound_shipping"], 0.0)

    def test_ebay_channel_uses_global_fees(self):
        from django.conf import settings
        preset = calculator.channel_preset("ebay")
        self.assertEqual(preset["platform_fee_pct"], settings.RESELLING_SCANNER["FEES"]["ebay_fee_pct"])

    def test_ebay_channel_jewelry_category_drops_fees(self):
        preset = calculator.channel_preset("ebay", category="jewelry")
        self.assertEqual(preset["platform_fee_pct"], 0.0)
        self.assertEqual(preset["packaging"], 0.0)


class PrefillFromLotTests(TestCase):
    def test_shipping_lot_prefill(self):
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="prefill-1", url="https://example.com/prefill-1",
            title="Lot", current_price=Decimal("42.00"), raw={},
        )
        data = calculator.prefill_from_lot(lot)
        self.assertEqual(data["bid"], 42.0)
        self.assertEqual(data["inbound_mode"], "shipping")

    def test_maxsold_pickup_lot_prefill_uses_lot_distance(self):
        lot = SourcedLot.objects.create(
            source="maxsold", external_id="prefill-2", url="https://example.com/prefill-2",
            title="Lot", current_price=Decimal("10.00"),
            raw={"_pickup": {"distance_miles": 12.5, "auction_id": 1, "auction_title": "x", "city": "Tampa",
                             "has_shipping": False}},
        )
        data = calculator.prefill_from_lot(lot)
        self.assertEqual(data["inbound_mode"], "pickup")
        self.assertEqual(data["pickup_miles"], 12.5)

    def test_expected_sale_prefers_ai_review_over_scanner(self):
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="prefill-3", url="https://example.com/prefill-3",
            title="Lot", current_price=Decimal("10.00"),
        )
        evaluation = LotEvaluation.objects.create(lot=lot, category="coins", expected_sale=Decimal("50.00"))
        review = AIReview.objects.create(
            lot=lot, status="done", resale_low=Decimal("80.00"), resale_high=Decimal("100.00"),
            cost_usd=Decimal("0.05"),
        )
        value = calculator.prefill_expected_sale(evaluation, review)
        self.assertEqual(value, 90.0)

    def test_expected_sale_falls_back_to_scanner_without_review(self):
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="prefill-4", url="https://example.com/prefill-4",
            title="Lot", current_price=Decimal("10.00"),
        )
        evaluation = LotEvaluation.objects.create(lot=lot, category="coins", expected_sale=Decimal("50.00"))
        self.assertEqual(calculator.prefill_expected_sale(evaluation, None), 50.0)

