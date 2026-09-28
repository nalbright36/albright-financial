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

    # Real false positives from the first full ShopGoodwill scan
    def test_non_coin_goods_excluded(self):
        for t in ["Vintage Astatic Silver Eagle Microphone",
                  "Kodak PIXPRO FZ45 Digital Camera Silver Eagle Creek Green Case Bundle",
                  "Eyes of Night Silver Eagle Figurine by Cynthie Fisher 2004 Bradford Ex",
                  "American Eagle Neon Yellow Hoodie M Medium Silver Eagle Graphic Fleece",
                  "Harley-Davidson 3XL Black Cotton T-Shirt Silver Eagle Palm Tree Logo",
                  "Silver Eagle Waterloo Iowa Graphic Tee Gray"]:
            r = parse_coin_text(t)
            self.assertEqual(r.items, [], t)

    def test_jewelry_excluded(self):
        for t in ["925 Sterling Silver Round Lab Created Sapphire Stud Earrings .78g No B",
                  "14k gold round button style earrings w/ small coins 2.8 grams"]:
            self.assertTrue(parse_coin_text(t).excluded_reason, t)

    def test_fineness_is_not_quantity(self):
        r = parse_coin_text("400 Silver Kennedy 1966 & 1967 Half Dollar Trio 34.34g")
        self.assertEqual((r.items[0].coin_key, r.items[0].quantity), ("kennedy_40", 3))

    def test_weight_sets_quantity(self):
        r = parse_coin_text("Kennedy Half Dollars 1964 lot 125.0g")
        self.assertEqual(r.items[0].quantity, 10)

    def test_weight_mismatch_is_low(self):
        r = parse_coin_text("1921 Morgan Silver Dollar 20.1g")
        self.assertEqual(r.confidence, "low")
        self.assertIn("weight_mismatch", r.flags)

    def test_key_date_is_low(self):
        r = parse_coin_text("Worn U.s 1916 30 Silver (90%) Standing Liberty Quarter")
        self.assertEqual((r.items[0].quantity, r.confidence), (1, "low"))

    def test_modern_gold_valued_conservatively(self):
        r = parse_coin_text("US 1991 $5 Gold Coin")
        self.assertEqual(r.items[0].coin_key, "modern_5_gold")
        self.assertAlmostEqual(r.total_oz("gold"), 0.1)
        self.assertEqual(parse_coin_text("1908 $5 Gold Coin Indian").items[0].coin_key, "half_eagle_5_gold")

    def test_fractional_gold_eagle(self):
        self.assertAlmostEqual(parse_coin_text("1/10 oz American Gold Eagle").total_oz("gold"), 0.1)
        self.assertEqual(parse_coin_text("American Gold Eagle coin").confidence, "low")

    def test_stated_percent_picks_composition(self):
        r = parse_coin_text("Kennedy Half Dollar 40% Silver US Mint Coins Lot of 10")
        self.assertEqual((r.items[0].coin_key, r.items[0].quantity), ("kennedy_40", 10))
        self.assertEqual(parse_coin_text("Kennedy Half Dollars 90% Silver lot of 4").items[0].coin_key, "kennedy_90")

    def test_year_range_is_not_key_date(self):
        r = parse_coin_text("Lot of 10 Vintage US Mercury Dimes 1916-1946 90% Silver")
        self.assertNotIn("key_date_verify", r.flags)
        self.assertEqual(r.confidence, "high")

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