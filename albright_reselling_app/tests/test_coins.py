"""Run with: python manage.py test albright_reselling_app.tests
(These tests don't touch the database, so they run fast.)"""
from unittest import TestCase

from albright_reselling_app.scanner.coins import parse_coin_text, estimate_resale
from albright_reselling_app.scanner.max_bid import SellFees, BuyCosts, max_bid, projected_profit

SPOT = {"silver": 30.0, "gold": 2500.0}


class CoinParserTests(TestCase):
    def test_morgan_lot_with_quantity(self):
        r = parse_coin_text("Lot of 5 Morgan Silver Dollars 1921")
        self.assertEqual(r.items[0].coin_key, "morgan_dollar")
        self.assertEqual(r.items[0].quantity, 5)
        self.assertEqual(r.confidence, "high")
        self.assertIn("numismatic_upside", r.flags)

    def test_real_shopgoodwill_title(self):
        r = parse_coin_text("Set of 2 Vintage 900 Morgan Silver Dollar Coins")
        self.assertEqual(r.items[0].coin_key, "morgan_dollar")
        self.assertEqual(r.items[0].quantity, 2)
        self.assertEqual(r.confidence, "high")

    def test_quantity_before_coin_name(self):
        self.assertEqual(parse_coin_text("10 Mercury Dimes 1940s").items[0].quantity, 10)

    def test_junk_silver_face_value(self):
        r = parse_coin_text("$10 Face 90% Silver Coins")
        self.assertAlmostEqual(r.total_oz("silver"), 7.15)

    def test_kennedy_year_decides_composition(self):
        self.assertEqual(parse_coin_text("1964 Kennedy Half Dollar").items[0].coin_key, "kennedy_90")
        self.assertEqual(parse_coin_text("1967 Kennedy Half Dollar").items[0].coin_key, "kennedy_40")
        self.assertEqual(parse_coin_text("1972 Kennedy Half Dollar").items, [])  # clad era

    def test_kennedy_without_year_is_low_confidence(self):
        self.assertEqual(parse_coin_text("Kennedy Half Dollars (10)").confidence, "low")

    def test_generic_bar(self):
        r = parse_coin_text("10 oz .999 Fine Silver Bar")
        self.assertAlmostEqual(r.total_oz("silver"), 10.0)

    def test_fractional_gold(self):
        r = parse_coin_text("1/10 oz .999 gold round")
        self.assertAlmostEqual(r.total_oz("gold"), 0.1)

    def test_exclusions(self):
        self.assertTrue(parse_coin_text("Morgan Dollar COPY").excluded_reason)
        self.assertTrue(parse_coin_text("Silver plated bar 1 oz").excluded_reason)
        self.assertTrue(parse_coin_text("Kennedy half clad lot").excluded_reason)

    def test_sterling_morgan_is_fake(self):
        r = parse_coin_text("Sterling Silver 925 1921 US Morgan Silver Dollar Coin Money")
        self.assertIn("sterling", r.excluded_reason)

    def test_impossible_year_is_fake(self):
        self.assertIn("never minted", parse_coin_text("900 Silver Antique 1925 US Morgan Dollar").excluded_reason)
        self.assertIn("never minted", parse_coin_text("1915 Morgan Silver Dollar").excluded_reason)
        self.assertFalse(parse_coin_text("1884-S Morgan Silver Dollar").excluded_reason)
        self.assertFalse(parse_coin_text("1922 Peace Dollar").excluded_reason)

    def test_sterling_bar_still_allowed(self):
        r = parse_coin_text("Franklin Mint .925 sterling silver bar 1 oz")
        self.assertFalse(r.excluded_reason)
        self.assertAlmostEqual(r.total_oz("silver"), 0.925)

    def test_franklin_mint_not_a_half(self):
        self.assertEqual(parse_coin_text("Franklin Mint sterling ingot").items, [])

    def test_mixed_lot_goes_to_llm(self):
        self.assertTrue(parse_coin_text("Morgan and Peace dollars mixed lot").needs_llm)

    def test_estimate_resale(self):
        r = parse_coin_text("(4) Silver Eagles")
        melt, expected = estimate_resale(r, SPOT, {"silver_eagle": 1.1})
        self.assertEqual(melt, 120.0)
        self.assertEqual(expected, 132.0)


class MaxBidTests(TestCase):
    def test_max_bid_leaves_target_profit(self):
        fees, buy = SellFees(), BuyCosts(buyer_premium_pct=0.1, sales_tax_pct=0.07, inbound_shipping=10)
        bid = max_bid(200.0, fees, buy)
        profit = projected_profit(bid, 200.0, fees, buy)
        self.assertAlmostEqual(profit, 40.0, delta=0.05)  # 20% of 200

    def test_never_negative(self):
        self.assertEqual(max_bid(10.0, SellFees(), BuyCosts(inbound_shipping=15)), 0.0)