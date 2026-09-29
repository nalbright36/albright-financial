"""Jewelry, games/cards leads, and the category router. No database needed.
Run with: python manage.py test albright_reselling_app.tests --keepdb"""
from unittest import TestCase

from albright_reselling_app.scanner.coins import estimate_resale
from albright_reselling_app.scanner.jewelry import parse_jewelry_text
from albright_reselling_app.scanner.leads import evaluate_lead
from albright_reselling_app.scanner.valuers import classify

LIMITS = {"games": 40, "cards": 40}


class JewelryTests(TestCase):
    def test_14k_with_grams(self):
        r = parse_jewelry_text("14k Yellow Gold Rope Chain Necklace 20in 6.2g")
        item = r.items[0]
        self.assertEqual((item.coin_key, item.metal, r.confidence), ("jewelry_gold_14k", "gold", "high"))
        self.assertAlmostEqual(item.oz_each, 6.2 * 0.583 / 31.1035, places=4)

    def test_stamp_and_dwt(self):
        r = parse_jewelry_text("585 gold ring 3 dwt")
        self.assertEqual(r.items[0].coin_key, "jewelry_gold_14k")
        self.assertAlmostEqual(r.items[0].oz_each, 3 * 1.555174 * 0.583 / 31.1035, places=4)

    def test_sterling_by_weight(self):
        r = parse_jewelry_text("Sterling Silver 925 Cuff Bracelet 42.5 grams")
        self.assertEqual((r.items[0].coin_key, r.items[0].metal), ("jewelry_sterling", "silver"))

    def test_lookalikes_excluded(self):
        for t in ["14k Gold Filled Chain 10g", "18k GE Ring size 7", "14K HGE Bracelet",
                  "Silver Plate Bracelet", "Gold Plated Necklace 5g"]:
            self.assertTrue(parse_jewelry_text(t).excluded_reason, t)
        # gold plating over sterling is still sterling
        for t in ["Gold Plated Sterling Necklace 5g", "Vermeil Earrings 925 3g"]:
            self.assertEqual(parse_jewelry_text(t).items[0].coin_key, "jewelry_sterling", t)

    def test_no_weight_is_review_lead(self):
        r = parse_jewelry_text("Vintage 14k Gold Diamond Ring Size 6")
        self.assertEqual(r.items, [])
        self.assertTrue(r.needs_review)
        self.assertIn("no weight", r.review_reason)

    def test_stones_reduce_weight(self):
        r = parse_jewelry_text("10k gold ring with sapphire 4.0g")
        self.assertEqual(r.confidence, "medium")
        self.assertAlmostEqual(r.items[0].oz_each, 3.6 * 0.417 / 31.1035, places=4)

    def test_mixed_karats_use_lowest(self):
        r = parse_jewelry_text("Scrap gold lot 10k and 14k rings 12.3g")
        self.assertEqual(r.items[0].coin_key, "jewelry_gold_10k")

    def test_scrap_lot_without_jewelry_word(self):
        r = parse_jewelry_text("14k gold scrap 7.8 grams")
        self.assertEqual(r.items[0].coin_key, "jewelry_gold_14k")

    def test_designer_flagged(self):
        r = parse_jewelry_text("Tiffany & Co Sterling Silver Heart Tag Bracelet 925 26g")
        self.assertTrue(r.needs_review)
        self.assertIn("designer:tiffany", r.flags)

    def test_resale_uses_family_multiplier(self):
        r = parse_jewelry_text("14k gold bracelet 31.1035g")
        melt, expected = estimate_resale(r, {"gold": 1000.0}, {"jewelry_gold": 0.8, "default": 1.0})
        self.assertAlmostEqual(melt, 583.0, delta=0.5)
        self.assertAlmostEqual(expected, 466.4, delta=0.5)


    # Real false positives from the first ShopGoodwill jewelry scan
    def test_gold_plate_over_sterling_is_sterling(self):
        r = parse_jewelry_text("4.6g Tycoon Designer 925 14K Rose Gold Plate Synthetic Topaz/CZ Eternity Ring")
        self.assertEqual(r.items[0].coin_key, "jewelry_sterling")

    def test_sterling_with_gold_accent_is_sterling(self):
        for t in ["925 Silver Tiffany & Co. Bracelet w/ 18KT Gold Accent - 40.5g",
                  "NK 925 Sterling & 18k Gold Foxtail Bracelet 18.25 Grams"]:
            self.assertEqual(parse_jewelry_text(t).items[0].metal, "silver", t)

    def test_base_metal_excluded(self):
        for t in ["Nomination 18k Gold Accent Stainless Steel Stretch Bracelet 13.2g",
                  "Stainless Steel Cufflinks with 18K Yellow Gold Medusa Head 9.5g"]:
            self.assertIn("base metal", parse_jewelry_text(t).excluded_reason, t)

    def test_implausible_ring_weight(self):
        r = parse_jewelry_text("18k Two Tone Gold Aquamarine Missing Stones Ring Size 73.4g")
        self.assertEqual(r.confidence, "low")
        self.assertIn("weight_implausible", r.flags)

    def test_8k_counts_as_lowest_karat(self):
        self.assertEqual(parse_jewelry_text("8K Yellow Gold Ring 6g Marked 18K").items[0].coin_key, "jewelry_gold_8k")

    def test_partial_sterling_lot_is_low(self):
        r = parse_jewelry_text("Vintage Cloisonne Jewelry SOME 925 Sterling Silver Lot 102.4g")
        self.assertEqual(r.confidence, "low")

    def test_rhodium_plated_sterling_still_sterling(self):
        self.assertEqual(parse_jewelry_text("Sterling Silver 925 Rhodium Plated Ring 5g").items[0].coin_key,
                         "jewelry_sterling")


class LeadTests(TestCase):
    def test_game_lot_is_lead(self):
        r = evaluate_lead("Lot of 8 Nintendo 64 N64 Games Mario Kart Zelda", "", 25.0, LIMITS)
        self.assertTrue(r.is_lead)
        self.assertEqual((r.category, r.subtype), ("games", "n64"))
        self.assertIn("bulk lot", r.signals)

    def test_over_limit_not_lead(self):
        r = evaluate_lead("GameCube Console Bundle with Games", "", 95.0, LIMITS)
        self.assertFalse(r.is_lead)
        self.assertEqual(r.category, "games")
        self.assertIn("over lead limit", r.excluded_reason)

    def test_single_common_game_not_lead(self):
        self.assertFalse(evaluate_lead("Madden 2005 PS2 Game", "", 3.0, LIMITS).is_lead)

    def test_card_binder_is_lead(self):
        r = evaluate_lead("Pokemon Card Binder Collection Holos Vintage WOTC", "", 30.0, LIMITS)
        self.assertTrue(r.is_lead)
        self.assertEqual(r.category, "cards")

    def test_graded_sports_card(self):
        r = evaluate_lead("1989 Upper Deck Ken Griffey Jr Rookie PSA 8", "", 20.0, LIMITS)
        self.assertTrue(r.is_lead)
        self.assertIn("graded", r.signals)

    def test_exclusions(self):
        for t in ["Lot Of Pokemon Commemorative Coins", "Pokemon Plush Lot", "Repro NES Cartridge Lot",
                  "Monopoly Board Game", "Vintage Playing Cards Deck Lot", "Pokemon T-Shirt"]:
            self.assertFalse(evaluate_lead(t, "", 5.0, LIMITS).is_lead, t)


class RouterTests(TestCase):
    def test_routes_each_category(self):
        self.assertEqual(classify("Lot of 10 Mercury Dimes 90% Silver", "", 20, LIMITS).category, "coins")
        self.assertEqual(classify("14k Gold Chain Necklace 5.1g", "", 20, LIMITS).category, "jewelry")
        self.assertEqual(classify("Lot of 12 Game Boy Games", "", 20, LIMITS).category, "games")
        self.assertEqual(classify("Box of 500 Baseball Cards Topps 1980s", "", 20, LIMITS).category, "cards")
        self.assertEqual(classify("Travando Men's Black Bifold Wallet", "", 2, LIMITS).category, "none")

    def test_coin_jewelry_goes_to_jewelry(self):
        # a coin pendant is excluded by the coin parser and valued as jewelry instead
        c = classify("14k gold pendant with coin 8.4g", "", 50, LIMITS)
        self.assertEqual(c.category, "jewelry")

    def test_plated_reason_kept(self):
        c = classify("18k Gold Plated Ring", "", 5, LIMITS)
        self.assertEqual(c.category, "none")
        self.assertIn("plated", c.reason)