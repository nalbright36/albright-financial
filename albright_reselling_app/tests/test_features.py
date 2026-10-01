"""Unit tests for scanner/features.py. No Django/DB/network involved - pure
function in, dict out - against sample ShopGoodwill and MaxSold item dicts
shaped like the real API responses (see scanner/adapters/shopgoodwill.py
and scanner/adapters/maxsold.py for the field names)."""
from unittest import TestCase

from albright_reselling_app.scanner.features import extract_features

SHOPGOODWILL_ITEM = {
    "itemId": 12345,
    "title": "14K Gold Ring 5.2 Grams Size 7",
    "catFullName": "Jewelry & Watches",
    "currentPrice": 45.0,
    "startingPrice": 1.0,
    "imageURL": "https://shopgoodwill.com/images/12345.jpg",
    "numBids": 8,
    "views": 312,
    "sellerId": 987,
    "shippingPrice": 9.99,
}

MAXSOLD_ITEM = {
    "amLotId": 55555,
    "title": "Estate Lot of Assorted Costume Jewelry",
    "description": "",
    "currentBid": {"amount": 20.0},
    "imageUrls": ["https://maxsold.com/a.jpg", "https://maxsold.com/b.jpg", ""],
    "amBidCount": 3,
    "amAuctionId": 777,
    "_pickup": {"distance_miles": 12.4, "auction_id": 777, "auction_title": "Estate of J. Smith",
                "city": "Tampa", "has_shipping": True},
}


class ExtractFeaturesShopGoodwillTests(TestCase):
    def test_title_signals_detected(self):
        features = extract_features(SHOPGOODWILL_ITEM, SHOPGOODWILL_ITEM["title"], "", "shopgoodwill")

        self.assertEqual(features["title_length"], len("14K Gold Ring 5.2 Grams Size 7"))
        self.assertEqual(features["title_word_count"], 7)
        self.assertFalse(features["title_all_caps"])
        self.assertTrue(features["title_has_weight"])
        self.assertTrue(features["title_has_karat_or_purity"])
        self.assertFalse(features["title_has_quantity"])
        self.assertFalse(features["title_vague"])

    def test_shopgoodwill_specific_fields(self):
        features = extract_features(SHOPGOODWILL_ITEM, SHOPGOODWILL_ITEM["title"], "", "shopgoodwill")

        self.assertEqual(features["photo_count"], 1)
        self.assertEqual(features["views"], 312)
        self.assertEqual(features["starting_price"], 1.0)
        self.assertEqual(features["seller_id"], 987)
        self.assertEqual(features["seller_category"], "Jewelry & Watches")
        self.assertEqual(features["shipping_price"], 9.99)
        self.assertIsNone(features["auction_id"])
        self.assertIsNone(features["distance_miles"])
        self.assertIsNone(features["has_shipping"])

    def test_no_image_means_zero_photos(self):
        item = dict(SHOPGOODWILL_ITEM)
        del item["imageURL"]

        features = extract_features(item, item["title"], "", "shopgoodwill")

        self.assertEqual(features["photo_count"], 0)

    def test_minimum_bid_fallback_for_starting_price(self):
        item = dict(SHOPGOODWILL_ITEM)
        del item["startingPrice"]
        item["minimumBid"] = 2.5

        features = extract_features(item, item["title"], "", "shopgoodwill")

        self.assertEqual(features["starting_price"], 2.5)

    def test_vague_title_with_no_weight_or_quantity(self):
        title = "Estate Grab Bag Misc Jewelry Lot"
        features = extract_features({}, title, "", "shopgoodwill")

        self.assertTrue(features["title_vague"])
        self.assertFalse(features["title_has_weight"])
        self.assertFalse(features["title_has_quantity"])

    def test_vague_word_with_quantity_stated_is_not_vague(self):
        title = "Lot of 10 Sterling Silver Rings"
        features = extract_features({}, title, "", "shopgoodwill")

        self.assertTrue(features["title_has_quantity"])
        self.assertFalse(features["title_vague"])

    def test_all_caps_title_detected(self):
        features = extract_features({}, "VINTAGE STERLING SILVER RING L@@K", "", "shopgoodwill")

        self.assertTrue(features["title_all_caps"])

    def test_quantity_patterns(self):
        for title in ("Lot of 5 Silver Coins", "Coin Set (12)", "Set of 4 Dinner Plates"):
            with self.subTest(title=title):
                self.assertTrue(extract_features({}, title, "", "shopgoodwill")["title_has_quantity"])


class ExtractFeaturesMaxSoldTests(TestCase):
    def test_photo_count_counts_nonempty_urls(self):
        features = extract_features(MAXSOLD_ITEM, MAXSOLD_ITEM["title"], "", "maxsold")

        self.assertEqual(features["photo_count"], 2)  # the trailing "" is skipped

    def test_maxsold_specific_fields(self):
        features = extract_features(MAXSOLD_ITEM, MAXSOLD_ITEM["title"], "", "maxsold")

        self.assertEqual(features["auction_id"], 777)
        self.assertEqual(features["distance_miles"], 12.4)
        self.assertTrue(features["has_shipping"])
        self.assertIsNone(features["views"])
        self.assertIsNone(features["seller_id"])
        self.assertIsNone(features["starting_price"])

    def test_vague_title_detected_for_maxsold_estate_lot(self):
        features = extract_features(MAXSOLD_ITEM, MAXSOLD_ITEM["title"], "", "maxsold")

        self.assertTrue(features["title_vague"])

    def test_description_length(self):
        features = extract_features(MAXSOLD_ITEM, MAXSOLD_ITEM["title"], "Hand-painted porcelain, no chips.",
                                     "maxsold")

        self.assertEqual(features["description_length"], len("Hand-painted porcelain, no chips."))

    def test_missing_description_is_zero_length(self):
        features = extract_features(MAXSOLD_ITEM, MAXSOLD_ITEM["title"], "", "maxsold")

        self.assertEqual(features["description_length"], 0)


class ExtractFeaturesEdgeCaseTests(TestCase):
    def test_empty_raw_dict_and_title_all_null(self):
        features = extract_features({}, "", "", "shopgoodwill")

        self.assertEqual(features["title_length"], 0)
        self.assertEqual(features["title_word_count"], 0)
        self.assertFalse(features["title_all_caps"])
        self.assertIsNone(features["views"])
        self.assertIsNone(features["seller_id"])
        self.assertIsNone(features["shipping_price"])

    def test_none_raw_dict_does_not_crash(self):
        features = extract_features(None, "Title", "Desc", "shopgoodwill")

        self.assertEqual(features["title_length"], 5)
        self.assertIsNone(features["seller_id"])

    def test_result_is_json_serializable(self):
        import json

        features = extract_features(MAXSOLD_ITEM, MAXSOLD_ITEM["title"], "desc", "maxsold")

        json.dumps(features)  # raises if anything isn't serializable
