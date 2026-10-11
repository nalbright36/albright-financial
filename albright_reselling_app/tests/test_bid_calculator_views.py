"""Tests for the Calculators page (calculator_views.py, formerly "Bid
Calculator"): the JSON endpoint for all three tabs, the Source dropdown's
presets, "Pick from Ledger", prefill from a scanner lot, the Melt value
tab (weight/junk modes, every unit, custom purity, spot prefill/override,
payout), and the no-JS POST fallback. Every number is checked against
scanner/simple_calc.py directly, since that is the only math this page is
allowed to use. Nav placement is covered in test_nav.py."""
import json
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from albright_reselling_app.models import LedgerEntry
from albright_reselling_app.scanner.simple_calc import (
    break_even_price, buy_cost, ebay_profit, junk_silver_value, melt_value, price_for_profit,
)
from albright_reselling_app.scanner_models import SourcedLot, SpotPrice


class CalculateApiBuyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_buy_tab_matches_simple_calc_buy_cost(self):
        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({
                "tab": "buy", "bid": "100", "premium_pct": "18", "tax_pct": "7", "shipping": "10",
                "other_fees": "2", "tax_on_premium": True, "tax_on_shipping": False,
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        expected = buy_cost(100, premium_pct=18, tax_pct=7, shipping=10, other_fees=2)
        self.assertEqual(data["bid"], expected.bid)
        self.assertEqual(data["premium"], expected.premium)
        self.assertEqual(data["tax"], expected.tax)
        self.assertEqual(data["total"], expected.total)
        self.assertIsNone(data["max_bid_for_budget"])

    def test_tax_on_shipping_toggle_changes_tax(self):
        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({
                "tab": "buy", "bid": "100", "tax_pct": "10", "shipping": "20", "tax_on_shipping": True,
            }),
            content_type="application/json",
        )
        data = response.json()
        expected = buy_cost(100, tax_pct=10, shipping=20, tax_on_shipping=True)
        self.assertEqual(data["tax"], expected.tax)
        self.assertGreater(data["tax"], 0)

    def test_budget_fills_max_bid_for_budget(self):
        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({"tab": "buy", "premium_pct": "18", "tax_pct": "7", "shipping": "10", "budget": "136.26"}),
            content_type="application/json",
        )
        data = response.json()
        self.assertIsNotNone(data["max_bid_for_budget"])
        capped = buy_cost(data["max_bid_for_budget"], premium_pct=18, tax_pct=7, shipping=10)
        self.assertLessEqual(capped.total, 136.26)

    def test_missing_budget_leaves_max_bid_none(self):
        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({"tab": "buy", "bid": "50"}),
            content_type="application/json",
        )
        self.assertIsNone(response.json()["max_bid_for_budget"])


class CalculateApiEbayTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_ebay_tab_matches_simple_calc_ebay_profit(self):
        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({
                "tab": "ebay", "item_cost": "40", "sale_price": "100", "shipping_cost": "6",
                "packaging": "1", "buyer_tax_pct": "7",
            }),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        expected = ebay_profit(40, 100, shipping_cost=6, packaging=1, buyer_tax_pct=7)
        self.assertEqual(data["profit"], expected.profit)
        self.assertEqual(data["payout"], expected.payout)
        self.assertEqual(data["final_value_fee"], expected.final_value_fee)

    def test_break_even_and_price_for_profit_included(self):
        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({
                "tab": "ebay", "item_cost": "40", "sale_price": "100", "shipping_cost": "6",
                "packaging": "1", "buyer_tax_pct": "7", "target_profit": "25",
            }),
            content_type="application/json",
        )
        data = response.json()
        kw = dict(shipping_cost=6, packaging=1, buyer_tax_pct=7)
        self.assertEqual(data["break_even_price"], break_even_price(40, **kw))
        self.assertEqual(data["price_for_profit"], price_for_profit(25, 40, **kw))

    def test_default_target_profit_is_20(self):
        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({"tab": "ebay", "item_cost": "40", "sale_price": "100"}),
            content_type="application/json",
        )
        self.assertEqual(response.json()["target_profit"], 20.0)

    def test_jewelry_category_uses_jewelry_tiers(self):
        most = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({"tab": "ebay", "item_cost": "10", "sale_price": "2000", "category": "most"}),
            content_type="application/json",
        ).json()
        jewelry = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({"tab": "ebay", "item_cost": "10", "sale_price": "2000", "category": "jewelry"}),
            content_type="application/json",
        ).json()
        self.assertNotEqual(most["final_value_fee"], jewelry["final_value_fee"])

    def test_custom_category_uses_editable_rate(self):
        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({
                "tab": "ebay", "item_cost": "10", "sale_price": "100", "buyer_tax_pct": "0",
                "category": "custom", "custom_rate_pct": "10",
            }),
            content_type="application/json",
        )
        data = response.json()
        self.assertEqual(data["final_value_fee"], round(100 * 0.10, 2))


class SourcePresetsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_page_embeds_source_defaults_from_settings(self):
        response = self.client.get(reverse("albright_reselling_app:calculator"))
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        start = content.index("var SOURCE_PRESETS = ") + len("var SOURCE_PRESETS = ")
        end = content.index(";", start)
        presets = json.loads(content[start:end])

        # shopgoodwill: 0% premium, 7% tax, $10 shipping (settings.py RESELLING_SCANNER)
        self.assertEqual(presets["shopgoodwill"], {"premium_pct": 0.0, "tax_pct": 7.0, "shipping": 10.0})
        # hibid uses "default_buyer_premium_pct" (not "buyer_premium_pct") - 20%
        self.assertEqual(presets["hibid"]["premium_pct"], 20.0)
        self.assertEqual(presets["hibid"]["shipping"], 15.0)
        # custom has no RESELLING_SCANNER entry - all zero
        self.assertEqual(presets["custom"], {"premium_pct": 0.0, "tax_pct": 0.0, "shipping": 0.0})


class LedgerPickTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_holding_and_partially_sold_items_included_with_buy_side_cost(self):
        holding = LedgerEntry.objects.create(
            owner=self.user, item="Holding Coin", cost=Decimal("50.00"), buyer_premium=Decimal("5.00"),
            status="holding",
        )
        partial = LedgerEntry.objects.create(
            owner=self.user, item="Partial Lot", cost=Decimal("20.00"), status="partially_sold",
        )
        LedgerEntry.objects.create(owner=self.user, item="Sold Out Item", cost=Decimal("30.00"), status="sold_out")
        LedgerEntry.objects.create(owner=self.user, item="Written Off Item", cost=Decimal("40.00"), status="written_off")

        response = self.client.get(reverse("albright_reselling_app:calculator"))
        content = response.content.decode()
        start = content.index("var LEDGER_ITEMS = ") + len("var LEDGER_ITEMS = ")
        end = content.index(";", start)
        items = json.loads(content[start:end])
        by_label = {item["label"]: item["cost"] for item in items}

        self.assertEqual(by_label["Holding Coin"], float(holding.buy_side_cost))
        self.assertEqual(by_label["Partial Lot"], float(partial.buy_side_cost))
        self.assertNotIn("Sold Out Item", by_label)
        self.assertNotIn("Written Off Item", by_label)

    def test_only_own_entries_included(self):
        other = User.objects.create_user(username="other", password="pw-not-real-12345")
        LedgerEntry.objects.create(owner=other, item="Someone Else's Item", cost=Decimal("10.00"), status="holding")

        response = self.client.get(reverse("albright_reselling_app:calculator"))

        self.assertNotContains(response, "Someone Else's Item")


class LotPrefillTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_lot_prefills_bid_and_source_defaults(self):
        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="calc-prefill-1", url="https://example.com/calc-prefill-1",
            title="Prefill Lot", current_price=Decimal("25.00"),
        )
        response = self.client.get(f"{reverse('albright_reselling_app:calculator')}?lot={lot.pk}")
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()

        start = content.index("var LOT_PREFILL = ") + len("var LOT_PREFILL = ")
        end = content.index(";", start)
        prefill = json.loads(content[start:end])

        self.assertEqual(prefill["bid"], 25.0)
        self.assertEqual(prefill["source"], "shopgoodwill")
        self.assertEqual(prefill["tax_pct"], 7.0)

    def test_no_lot_param_gives_empty_prefill(self):
        response = self.client.get(reverse("albright_reselling_app:calculator"))
        content = response.content.decode()
        start = content.index("var LOT_PREFILL = ") + len("var LOT_PREFILL = ")
        end = content.index(";", start)
        self.assertEqual(json.loads(content[start:end]), {})


class NoJsPostFallbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("albright_reselling_app:calculator"))
        self.assertEqual(response.status_code, 302)

    def test_page_loads_with_both_tabs(self):
        response = self.client.get(reverse("albright_reselling_app:calculator"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Calculators")
        self.assertContains(response, "Buy cost")
        self.assertContains(response, "eBay profit")
        self.assertContains(response, "Melt value")

    def test_buy_tab_post_renders_breakdown(self):
        response = self.client.post(reverse("albright_reselling_app:calculator"), {
            "tab": "buy", "bid": "100", "premium_pct": "18", "tax_pct": "7", "shipping": "10",
            "other_fees": "0", "tax_on_premium": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Out-the-door total")
        expected = buy_cost(100, premium_pct=18, tax_pct=7, shipping=10)
        self.assertContains(response, f"${expected.total:.2f}")

    def test_ebay_tab_post_renders_profit(self):
        response = self.client.post(reverse("albright_reselling_app:calculator"), {
            "tab": "ebay", "item_cost": "40", "sale_price": "100", "shipping_cost": "6",
            "packaging": "1", "buyer_tax_pct": "7", "category": "most",
        })
        self.assertEqual(response.status_code, 200)
        expected = ebay_profit(40, 100, shipping_cost=6, packaging=1, buyer_tax_pct=7)
        self.assertContains(response, f"${expected.profit:.2f}")


class CalculateApiMeltTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def _post(self, **fields):
        data = {"tab": "melt", **fields}
        return self.client.post(
            reverse("albright_reselling_app:calculator_calculate"), data=json.dumps(data),
            content_type="application/json",
        )

    def test_by_weight_grams_matches_simple_calc(self):
        response = self._post(mode="weight", metal="gold", purity="0.583", weight="10", unit="g", spot="2650")

        self.assertEqual(response.status_code, 200)
        expected = melt_value(10, "g", 0.583, 2650)
        data = response.json()
        self.assertEqual(data["melt_value"], expected.melt_value)
        self.assertEqual(data["pure_ozt"], expected.pure_ozt)
        self.assertEqual(data["melt_per_gram"], expected.melt_per_gram)

    def test_every_unit_matches_simple_calc(self):
        for unit in ("g", "dwt", "ozt", "oz"):
            response = self._post(mode="weight", metal="silver", purity="0.925", weight="5", unit=unit, spot="31.5")
            expected = melt_value(5, unit, 0.925, 31.5)
            self.assertEqual(response.json()["melt_value"], expected.melt_value, f"unit={unit}")

    def test_custom_purity(self):
        response = self._post(
            mode="weight", metal="gold", purity="custom", custom_purity="0.611", weight="3", unit="g", spot="2650",
        )

        expected = melt_value(3, "g", 0.611, 2650)
        self.assertEqual(response.json()["melt_value"], expected.melt_value)

    def test_junk_silver_mode(self):
        response = self._post(mode="junk", metal="silver", face_value="10", spot="31.5")

        self.assertEqual(response.status_code, 200)
        expected = junk_silver_value(10, 31.5)
        data = response.json()
        self.assertEqual(data["melt_value"], expected.melt_value)
        self.assertEqual(data["pure_ozt"], expected.pure_ozt)

    def test_payout_percent_applied(self):
        response = self._post(mode="weight", metal="gold", purity="0.583", weight="10", unit="g", spot="2650",
                               payout_pct="80")

        expected = melt_value(10, "g", 0.583, 2650, payout_pct=80)
        data = response.json()
        self.assertEqual(data["payout_value"], expected.payout_value)
        self.assertLess(data["payout_value"], data["melt_value"])

    def test_default_payout_is_100(self):
        response = self._post(mode="weight", metal="gold", purity="0.583", weight="10", unit="g", spot="2650")

        data = response.json()
        self.assertEqual(data["payout_value"], data["melt_value"])


class MeltSpotPrefillTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_page_embeds_latest_spot_price_and_date(self):
        SpotPrice.objects.create(metal="gold", price_usd=Decimal("2650.00"), source="test")
        SpotPrice.objects.create(metal="silver", price_usd=Decimal("31.50"), source="test")

        response = self.client.get(reverse("albright_reselling_app:calculator"))
        content = response.content.decode()

        start = content.index("var LATEST_SPOT = ") + len("var LATEST_SPOT = ")
        end = content.index(";", start)
        spot = json.loads(content[start:end])

        self.assertEqual(spot["gold"]["price"], 2650.0)
        self.assertEqual(spot["silver"]["price"], 31.5)
        self.assertIsNotNone(spot["gold"]["date"])

    def test_latest_spot_price_used_when_multiple_exist(self):
        SpotPrice.objects.create(metal="gold", price_usd=Decimal("2600.00"), source="test")
        SpotPrice.objects.create(metal="gold", price_usd=Decimal("2650.00"), source="test")  # inserted later = latest

        response = self.client.get(reverse("albright_reselling_app:calculator"))
        content = response.content.decode()
        start = content.index("var LATEST_SPOT = ") + len("var LATEST_SPOT = ")
        end = content.index(";", start)
        spot = json.loads(content[start:end])

        self.assertEqual(spot["gold"]["price"], 2650.0)

    def test_no_spot_price_on_file_gives_null(self):
        SpotPrice.objects.filter(metal="gold").delete()

        response = self.client.get(reverse("albright_reselling_app:calculator"))
        content = response.content.decode()
        start = content.index("var LATEST_SPOT = ") + len("var LATEST_SPOT = ")
        end = content.index(";", start)
        spot = json.loads(content[start:end])

        self.assertIsNone(spot["gold"]["price"])
        self.assertIsNone(spot["gold"]["date"])

    def test_edited_spot_value_overrides_prefill_for_the_calculation(self):
        SpotPrice.objects.create(metal="gold", price_usd=Decimal("2650.00"), source="test")

        response = self.client.post(
            reverse("albright_reselling_app:calculator_calculate"),
            data=json.dumps({
                "tab": "melt", "mode": "weight", "metal": "gold", "purity": "0.583", "weight": "10", "unit": "g",
                "spot": "3000",  # manually overridden - should be used, not the stored 2650 price
            }),
            content_type="application/json",
        )

        expected = melt_value(10, "g", 0.583, 3000)
        self.assertEqual(response.json()["melt_value"], expected.melt_value)


class MeltNoJsPostTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_melt_tab_post_renders_result(self):
        response = self.client.post(reverse("albright_reselling_app:calculator"), {
            "tab": "melt", "mode": "weight", "metal": "gold", "purity": "0.583", "weight": "10", "unit": "g",
            "spot": "2650", "payout_pct": "100",
        })

        self.assertEqual(response.status_code, 200)
        expected = melt_value(10, "g", 0.583, 2650)
        self.assertContains(response, f"${expected.melt_value:.2f}")

    def test_melt_tab_is_active_after_its_own_post(self):
        response = self.client.post(reverse("albright_reselling_app:calculator"), {
            "tab": "melt", "mode": "junk", "metal": "silver", "face_value": "10", "spot": "31.5",
        })

        self.assertContains(response, 'id="tab-melt"')
        self.assertNotContains(response, 'id="tab-melt" hidden')


class CalculatorNavAndUrlTests(TestCase):
    """The nav label changed from "Bid Calculator" to "Calculators" but
    every URL (page + JSON endpoint) is unchanged - old bookmarks/links
    keep working."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_old_calculator_url_still_resolves(self):
        response = self.client.get("/albright_reselling_app/calculator/")

        self.assertEqual(response.status_code, 200)

    def test_old_calculate_api_url_still_resolves(self):
        response = self.client.post(
            "/albright_reselling_app/calculator/calculate/",
            data=json.dumps({"tab": "buy", "bid": "10"}), content_type="application/json",
        )

        self.assertEqual(response.status_code, 200)

    def test_nav_shows_calculators_not_bid_calculator(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, ">Calculators<")
        self.assertNotContains(response, "Bid Calculator")
