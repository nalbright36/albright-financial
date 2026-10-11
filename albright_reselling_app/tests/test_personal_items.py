"""Tests for the Personal Items ledger (albright_reselling_app): the
ledger_type field/exclude_personal() filter and its use in every
reselling calculation, the Personal Items tab's own totals, the Overview
tab's cross-ledger totals, the 3 tab URLs plus the legacy /ledger/
redirect, and the Bid Calculator's "Pick from Ledger" including personal
items at $0."""
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app import ledger_metrics
from albright_reselling_app.calculator_views import _ledger_pick_items
from albright_reselling_app.models import LedgerEntry, LedgerSale


def _make_entry(owner, item="Test Item", cost="0.00", status="holding", ledger_type="resale",
                 purchase_date=None, **extra):
    entry = LedgerEntry.objects.create(
        owner=owner, item=item, cost=Decimal(cost), status=status, ledger_type=ledger_type,
        purchase_date=purchase_date or timezone.localdate(), **extra,
    )
    entry.refresh_from_db()
    return entry


class LedgerTypeDefaultTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_new_entry_defaults_to_resale(self):
        entry = LedgerEntry.objects.create(owner=self.user, item="Plain Entry")

        self.assertEqual(entry.ledger_type, "resale")

    def test_exclude_personal_filters_queryset(self):
        _make_entry(self.user, item="Resale", ledger_type="resale")
        _make_entry(self.user, item="Personal", ledger_type="personal")

        filtered = ledger_metrics.exclude_personal(LedgerEntry.objects.filter(owner=self.user))

        self.assertEqual(list(filtered.values_list("item", flat=True)), ["Resale"])


class ExcludePersonalFromResellingMetricsTests(TestCase):
    """Personal entries must never affect any reselling calculation -
    dashboard tiles, Scorecard, aging/markdown counts."""

    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_dashboard_tiles_exclude_personal_inventory_and_profit(self):
        _make_entry(self.user, item="Resale Holding", cost="50.00", status="holding")
        _make_entry(self.user, item="Personal Holding", cost="0.00", status="holding", ledger_type="personal")
        today = timezone.localdate()
        _make_entry(self.user, item="Resale Sold", cost="10.00", sold_for="40.00", status="sold_out",
                    sold_date=today)
        personal_sold = _make_entry(self.user, item="Personal Sold", status="sold_out", ledger_type="personal")
        LedgerSale.objects.create(entry=personal_sold, sale_date=today, sale_price=Decimal("500.00"))

        tiles = ledger_metrics.dashboard_tiles(self.user)

        self.assertEqual(tiles["inventory_count"], 1)  # only the resale holding item
        self.assertEqual(tiles["inventory_cost"], Decimal("50.00"))
        self.assertEqual(tiles["all_time_profit"], Decimal("30.00"))  # 40-10 only, personal's 500 excluded
        self.assertEqual(tiles["profit_this_month"], Decimal("30.00"))

    def test_scorecard_excludes_personal_entries(self):
        _make_entry(self.user, item="Resale", cost="10.00", sold_for="20.00", status="sold_out")
        _make_entry(self.user, item="Personal", status="holding", ledger_type="personal")

        context = ledger_metrics.scorecard_context(self.user)

        items = [e.item for e in context["entries"]]
        self.assertIn("Resale", items)
        self.assertNotIn("Personal", items)
        self.assertEqual(context["unsold_count"], 0)

    def test_scorecard_aging_and_markdown_exclude_personal(self):
        _make_entry(self.user, item="Resale Stale", cost="10.00", status="holding",
                    purchase_date=timezone.localdate() - timedelta(days=95))
        _make_entry(self.user, item="Personal Stale", status="holding", ledger_type="personal",
                    purchase_date=timezone.localdate() - timedelta(days=95))

        scorecard = ledger_metrics.scorecard_context(self.user)
        dashboard = ledger_metrics.dashboard_tiles(self.user)

        aging_items = [row["entry"].item for row in scorecard["aging"]]
        self.assertEqual(aging_items, ["Resale Stale"])
        self.assertEqual(dashboard["markdown_count"], 1)

    def test_realized_summary_excludes_personal(self):
        today = timezone.localdate()
        _make_entry(self.user, item="Resale Sold", cost="10.00", sold_for="30.00", status="sold_out",
                    sold_date=today)
        personal = _make_entry(self.user, item="Personal Sold", status="sold_out", ledger_type="personal")
        LedgerSale.objects.create(entry=personal, sale_date=today, sale_price=Decimal("1000.00"))

        summary = ledger_metrics.realized_summary(self.user)

        self.assertEqual(summary["sold_this_month"], 1)
        self.assertEqual(summary["realized_profit_this_month"], Decimal("20.00"))

    def test_net_cash_flow_defaults_to_resale_only(self):
        _make_entry(self.user, item="Resale", cost="20.00", status="holding")
        _make_entry(self.user, item="Personal", status="holding", ledger_type="personal")

        result = ledger_metrics.net_cash_flow(self.user)

        self.assertEqual(result["costs"], Decimal("20.00"))

    def test_calibration_query_excludes_personal_entries(self):
        """scanner/insights.py's _ledger_multiplier_suggestions() queries
        LedgerEntry directly (not owner-scoped) with exactly this filter
        (melt_value set + a scanner_lot) - exclude_personal() must still
        remove a personal row that otherwise matches every criterion."""
        from albright_reselling_app.scanner_models import SourcedLot

        lot = SourcedLot.objects.create(
            source="shopgoodwill", external_id="calib-personal-1", url="https://example.com/calib-personal-1",
            title="Lot", current_price=Decimal("10.00"),
        )
        personal = _make_entry(
            self.user, item="Personal with melt+lot", status="sold_out", ledger_type="personal",
            melt_value=Decimal("10.00"), scanner_lot=lot,
        )

        matching = ledger_metrics.exclude_personal(
            LedgerEntry.objects.filter(melt_value__isnull=False, melt_value__gt=0, scanner_lot__isnull=False)
        )

        self.assertNotIn(personal.pk, list(matching.values_list("pk", flat=True)))


class PersonalItemsTabTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_personal_items_context_tiles(self):
        today = timezone.localdate()
        holding = _make_entry(self.user, item="Still Listed", status="holding", ledger_type="personal")
        sold = _make_entry(self.user, item="Sold Item", status="sold_out", ledger_type="personal")
        LedgerSale.objects.create(
            entry=sold, sale_date=today, sale_price=Decimal("100.00"), selling_fees=Decimal("10.00"),
            outbound_shipping=Decimal("8.00"), packaging=Decimal("2.00"),
        )

        context = ledger_metrics.personal_items_context(self.user)

        self.assertEqual(context["tiles"]["revenue_all_time"], Decimal("100.00"))
        self.assertEqual(context["tiles"]["revenue_this_month"], Decimal("100.00"))
        self.assertEqual(context["tiles"]["net_after_fees"], Decimal("80.00"))  # 100 - 10 - 8 - 2
        self.assertEqual(context["tiles"]["items_listed_count"], 1)
        self.assertEqual(len(context["aging"]), 1)
        self.assertEqual(context["aging"][0]["entry"].item, "Still Listed")

    def test_quick_add_creates_personal_entry_with_zero_buy_cost(self):
        response = self.client.post(reverse("albright_reselling_app:ledger_personal"), {
            "add_entry": "1", "add-item": "Vintage Lamp", "add-notes": "small chip on base",
            "add-asking_price": "45.00",
        })

        self.assertRedirects(response, reverse("albright_reselling_app:ledger_personal"))
        entry = LedgerEntry.objects.get(item="Vintage Lamp")
        self.assertEqual(entry.ledger_type, "personal")
        self.assertEqual(entry.owner, self.user)
        self.assertEqual(entry.cost, Decimal("0.00"))
        self.assertEqual(entry.buy_side_cost, Decimal("0.00"))
        self.assertEqual(entry.notes, "small chip on base")
        self.assertEqual(entry.asking_price, Decimal("45.00"))

    def test_personal_table_totals_match_context(self):
        today = timezone.localdate()
        entry = _make_entry(self.user, item="Sold Thing", status="sold_out", ledger_type="personal")
        LedgerSale.objects.create(
            entry=entry, sale_date=today, sale_price=Decimal("60.00"), selling_fees=Decimal("5.00"),
            outbound_shipping=Decimal("4.00"), packaging=Decimal("1.00"),
        )

        response = self.client.get(reverse("albright_reselling_app:ledger_personal"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "$60.00")  # sold for
        self.assertContains(response, "$50.00")  # net: 60-5-4-1

    def test_record_sale_works_on_personal_entry(self):
        entry = _make_entry(self.user, item="Old Chair", status="holding", ledger_type="personal")

        response = self.client.post(reverse("albright_reselling_app:ledger_record_sale", args=[entry.pk]), {
            "sale_date": timezone.localdate().isoformat(), "sale_price": "35.00", "channel": "local_facebook",
            "selling_fees": "0", "outbound_shipping": "0", "packaging": "0", "hours_spent": "0", "notes": "",
        })

        self.assertRedirects(response, reverse("albright_reselling_app:ledger_manage", args=[entry.pk]))
        entry.refresh_from_db()
        self.assertEqual(entry.status, "partially_sold")
        self.assertEqual(entry.total_realized, Decimal("35.00"))


class OverviewTabTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_totals_add_up_across_both_ledgers(self):
        today = timezone.localdate()
        _make_entry(self.user, item="Resale Sold", cost="10.00", sold_for="40.00", status="sold_out",
                    sold_date=today)
        _make_entry(self.user, item="Resale Holding", cost="5.00", status="holding")
        personal_sold = _make_entry(self.user, item="Personal Sold", status="sold_out", ledger_type="personal")
        LedgerSale.objects.create(entry=personal_sold, sale_date=today, sale_price=Decimal("20.00"))
        _make_entry(self.user, item="Personal Holding", status="holding", ledger_type="personal")

        context = ledger_metrics.overview_context(self.user)

        resale_row = next(r for r in context["rows"] if r["label"] == "Reselling")
        personal_row = next(r for r in context["rows"] if r["label"] == "Personal")
        total_row = context["total_row"]

        self.assertEqual(resale_row["revenue"], Decimal("40.00"))
        self.assertEqual(resale_row["costs"], Decimal("15.00"))  # 10 (sold) + 5 (holding)
        self.assertEqual(personal_row["revenue"], Decimal("20.00"))
        self.assertEqual(personal_row["costs"], Decimal("0.00"))

        self.assertEqual(total_row["revenue"], resale_row["revenue"] + personal_row["revenue"])
        self.assertEqual(total_row["costs"], resale_row["costs"] + personal_row["costs"])
        self.assertEqual(total_row["net"], resale_row["net"] + personal_row["net"])
        self.assertEqual(total_row["items_sold"], resale_row["items_sold"] + personal_row["items_sold"])
        self.assertEqual(total_row["items_held"], resale_row["items_held"] + personal_row["items_held"])

        self.assertEqual(context["tiles"]["total_revenue"], total_row["revenue"])
        self.assertEqual(context["tiles"]["total_costs"], total_row["costs"])
        self.assertEqual(context["tiles"]["net"], total_row["net"])
        self.assertEqual(context["tiles"]["items_sold"], total_row["items_sold"])

    def test_monthly_breakdown_attributes_sale_to_its_own_month(self):
        now = timezone.now()
        last_month_date = (timezone.localdate().replace(day=1) - timedelta(days=1))
        resale = _make_entry(self.user, item="Sold Last Month", cost="10.00", status="sold_out")
        LedgerSale.objects.create(entry=resale, sale_date=last_month_date, sale_price=Decimal("50.00"))

        context = ledger_metrics.overview_context(self.user, now=now)

        matching = [row for row in context["monthly_rows"] if row["month"].month == last_month_date.month
                    and row["month"].year == last_month_date.year]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["resale_revenue"], Decimal("50.00"))
        self.assertEqual(matching[0]["resale_net"], Decimal("40.00"))

        this_month_rows = [row for row in context["monthly_rows"]
                            if row["month"].month == now.month and row["month"].year == now.year]
        self.assertEqual(this_month_rows[0]["resale_revenue"], Decimal("0"))

    def test_overview_page_renders(self):
        response = self.client.get(reverse("albright_reselling_app:ledger_overview"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Overview")
        self.assertContains(response, "Reselling")
        self.assertContains(response, "Personal")
        self.assertContains(response, "Last 12 Months")


class LedgerTabUrlTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_legacy_ledger_url_redirects_to_resale(self):
        response = self.client.get(reverse("albright_reselling_app:ledger"))

        self.assertRedirects(response, reverse("albright_reselling_app:ledger_resale"))

    def test_legacy_ledger_url_preserves_query_string(self):
        response = self.client.get(reverse("albright_reselling_app:ledger") + "?status=unsold")

        self.assertRedirects(response, reverse("albright_reselling_app:ledger_resale") + "?status=unsold")

    def test_all_three_tabs_load(self):
        for name in ("ledger_overview", "ledger_resale", "ledger_personal"):
            response = self.client.get(reverse(f"albright_reselling_app:{name}"))
            self.assertEqual(response.status_code, 200, f"{name} did not load")

    def test_tab_bar_links_present_on_all_three_pages(self):
        for name in ("ledger_overview", "ledger_resale", "ledger_personal"):
            response = self.client.get(reverse(f"albright_reselling_app:{name}"))
            self.assertContains(response, reverse("albright_reselling_app:ledger_overview"))
            self.assertContains(response, reverse("albright_reselling_app:ledger_resale"))
            self.assertContains(response, reverse("albright_reselling_app:ledger_personal"))

    def test_resale_tab_only_shows_resale_entries(self):
        _make_entry(self.user, item="Resale Item", ledger_type="resale")
        _make_entry(self.user, item="Personal Item", ledger_type="personal")

        response = self.client.get(reverse("albright_reselling_app:ledger_resale"))

        self.assertContains(response, "Resale Item")
        self.assertNotContains(response, "Personal Item")

    def test_personal_tab_only_shows_personal_entries(self):
        _make_entry(self.user, item="Resale Item", ledger_type="resale")
        _make_entry(self.user, item="Personal Item", ledger_type="personal")

        response = self.client.get(reverse("albright_reselling_app:ledger_personal"))

        self.assertContains(response, "Personal Item")
        self.assertNotContains(response, "Resale Item")


class BidCalculatorPersonalItemsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_pick_from_ledger_includes_personal_items_at_zero_cost(self):
        _make_entry(self.user, item="Old Guitar", status="holding", ledger_type="personal")
        _make_entry(self.user, item="Resale Coin", cost="25.00", status="holding", ledger_type="resale")

        items = _ledger_pick_items(self.user)
        by_label = {item["label"]: item["cost"] for item in items}

        self.assertEqual(by_label["Old Guitar (personal)"], 0.0)
        self.assertEqual(by_label["Resale Coin"], 25.0)

    def test_sold_out_personal_items_excluded_from_picker(self):
        _make_entry(self.user, item="Sold Guitar", status="sold_out", ledger_type="personal")

        items = _ledger_pick_items(self.user)

        self.assertNotIn("Sold Guitar (personal)", [item["label"] for item in items])
