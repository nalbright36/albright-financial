"""Bid Calculator math (scanner/simple_calc.py). No database needed."""
from unittest import TestCase

from albright_reselling_app.scanner.simple_calc import (
    EBAY_PRESETS, break_even_price, buy_cost, ebay_profit, max_bid_for_total, price_for_profit, tiered_fee)


class BuyCostTests(TestCase):
    def test_maxsold_style(self):
        # $100 bid, 18% premium, 7% tax on bid+premium, $10 shipping
        c = buy_cost(100, premium_pct=18, tax_pct=7, shipping=10)
        self.assertEqual((c.premium, c.tax, c.total), (18.0, 8.26, 136.26))

    def test_tax_not_on_premium(self):
        self.assertEqual(buy_cost(100, 18, 7, tax_on_premium=False).tax, 7.0)

    def test_max_bid_for_total_stays_under_budget(self):
        bid = max_bid_for_total(136.26, premium_pct=18, tax_pct=7, shipping=10)
        self.assertLessEqual(buy_cost(bid, 18, 7, shipping=10).total, 136.26)
        self.assertGreater(buy_cost(bid + 0.02, 18, 7, shipping=10).total, 136.26)


class EbayTests(TestCase):
    def test_example_from_ebay_rules(self):
        # $100 item, $8 shipping charged, no tax, most categories: 13.6% of $108 + $0.40
        r = ebay_profit(0, 100, shipping_charged=8, buyer_tax_pct=0)
        self.assertEqual((r.final_value_fee, r.per_order_fee, r.total_ebay_fees), (14.69, 0.40, 15.09))

    def test_fee_includes_buyer_tax(self):
        r = ebay_profit(0, 100, buyer_tax_pct=7)
        self.assertEqual((r.fee_base, r.final_value_fee), (107.0, 14.55))

    def test_small_order_fee(self):
        self.assertEqual(ebay_profit(0, 8, buyer_tax_pct=0).per_order_fee, 0.30)

    def test_jewelry_tiers(self):
        tiers = EBAY_PRESETS["jewelry"]["tiers"]
        self.assertAlmostEqual(tiered_fee(2000, tiers), 1000 * 0.15 + 1000 * 0.065)

    def test_profit_math(self):
        r = ebay_profit(item_cost=40, sale_price=100, shipping_cost=6, packaging=1, buyer_tax_pct=7)
        self.assertEqual(r.profit, round(100 - (14.55 + 0.40) - 6 - 1 - 40, 2))
        self.assertEqual(r.payout, 85.05)

    def test_break_even_and_target(self):
        kw = dict(shipping_cost=6, packaging=1, buyer_tax_pct=7)
        be = break_even_price(40, **kw)
        self.assertGreaterEqual(ebay_profit(40, be, **kw).profit, 0)
        self.assertLess(ebay_profit(40, round(be - 0.01, 2), **kw).profit, 0)
        p = price_for_profit(25, 40, **kw)
        self.assertGreaterEqual(ebay_profit(40, p, **kw).profit, 25)
        self.assertLess(ebay_profit(40, round(p - 0.01, 2), **kw).profit, 25)

    def test_promoted_listing_fee(self):
        self.assertEqual(ebay_profit(0, 100, buyer_tax_pct=0, promoted_pct=5).promoted_fee, 5.0)