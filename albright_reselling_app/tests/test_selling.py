"""Tests for the Today page and Selling Board (selling.py/selling_views.py):
board column placement, the mark-listed/record-sale/mark-shipped/markdown
action flows, the shared markdown/aging definition (used by both the
dashboard's Needs Action tile and the Ledger aging views), sell-goal
progress, the Today page's section visibility, and the Today/Board nav
wiring and landing redirect."""
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app import ledger_metrics, selling
from albright_reselling_app.models import LedgerEntry, LedgerSale


def _make_entry(owner, item="Test Item", cost="10.00", status="holding", purchase_date=None, **extra):
    entry = LedgerEntry.objects.create(
        owner=owner, item=item, cost=Decimal(cost), status=status,
        purchase_date=purchase_date or timezone.localdate(), **extra,
    )
    entry.refresh_from_db()
    return entry


def _make_sale(entry, sale_price="20.00", sale_date=None, shipped_at=None):
    return LedgerSale.objects.create(
        entry=entry, sale_date=sale_date or timezone.localdate(), sale_price=Decimal(sale_price),
        shipped_at=shipped_at,
    )


# ---------------------------------------------------------------------------
# Board columns
# ---------------------------------------------------------------------------

class BoardColumnsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_unlisted_holding_entry_is_to_list(self):
        entry = _make_entry(self.user, status="holding")

        columns = selling.board_columns(self.user)

        self.assertEqual([c["entry"] for c in columns["to_list"]], [entry])
        self.assertEqual(columns["listed"], [])
        self.assertEqual(columns["to_ship"], [])
        self.assertEqual(columns["shipped"], [])

    def test_listed_holding_entry_is_listed_column(self):
        entry = _make_entry(self.user, status="holding", listed_at=timezone.localdate() - timedelta(days=5))

        columns = selling.board_columns(self.user)

        self.assertEqual([c["entry"] for c in columns["listed"]], [entry])

    def test_sold_with_unshipped_sale_is_to_ship(self):
        entry = _make_entry(self.user, status="partially_sold")
        _make_sale(entry, shipped_at=None)

        columns = selling.board_columns(self.user)

        self.assertEqual([c["entry"] for c in columns["to_ship"]], [entry])

    def test_sold_with_one_shipped_one_unshipped_sale_is_still_to_ship(self):
        entry = _make_entry(self.user, status="sold_out")
        _make_sale(entry, sale_price="10", shipped_at=timezone.now())
        _make_sale(entry, sale_price="15", shipped_at=None)

        columns = selling.board_columns(self.user)

        self.assertEqual([c["entry"] for c in columns["to_ship"]], [entry])

    def test_sold_and_shipped_within_30_days_is_shipped_column(self):
        entry = _make_entry(self.user, status="sold_out")
        _make_sale(entry, shipped_at=timezone.now() - timedelta(days=5))

        columns = selling.board_columns(self.user)

        self.assertEqual([c["entry"] for c in columns["shipped"]], [entry])
        self.assertEqual(columns["to_ship"], [])

    def test_shipped_more_than_30_days_ago_drops_off_the_board_entirely(self):
        entry = _make_entry(self.user, status="sold_out")
        _make_sale(entry, shipped_at=timezone.now() - timedelta(days=40))

        columns = selling.board_columns(self.user)

        all_entries = [c["entry"] for cards in columns.values() for c in cards]
        self.assertNotIn(entry, all_entries)

    def test_legacy_sold_out_with_no_itemized_sales_excluded_entirely(self):
        entry = _make_entry(self.user, status="sold_out", sold_for="40.00")

        columns = selling.board_columns(self.user)

        all_entries = [c["entry"] for cards in columns.values() for c in cards]
        self.assertNotIn(entry, all_entries)

    def test_ledger_type_filter(self):
        resale_entry = _make_entry(self.user, item="Resale", status="holding", ledger_type="resale")
        personal_entry = _make_entry(self.user, item="Personal", status="holding", ledger_type="personal")

        resale_columns = selling.board_columns(self.user, ledger_type="resale")
        personal_columns = selling.board_columns(self.user, ledger_type="personal")

        self.assertEqual([c["entry"] for c in resale_columns["to_list"]], [resale_entry])
        self.assertEqual([c["entry"] for c in personal_columns["to_list"]], [personal_entry])

    def test_columns_sorted_oldest_first(self):
        newer = _make_entry(self.user, item="Newer", purchase_date=timezone.localdate() - timedelta(days=2))
        older = _make_entry(self.user, item="Older", purchase_date=timezone.localdate() - timedelta(days=20))

        columns = selling.board_columns(self.user)

        self.assertEqual([c["entry"] for c in columns["to_list"]], [older, newer])


# ---------------------------------------------------------------------------
# Mark listed / record sale / mark shipped flows
# ---------------------------------------------------------------------------

class MarkListedFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_get_renders_form(self):
        entry = _make_entry(self.user, status="holding")

        response = self.client.get(reverse("albright_reselling_app:ledger_mark_listed", args=[entry.pk]))

        self.assertEqual(response.status_code, 200)

    def test_post_sets_listed_at_to_today_and_fields(self):
        entry = _make_entry(self.user, status="holding")

        response = self.client.post(
            reverse("albright_reselling_app:ledger_mark_listed", args=[entry.pk]),
            {"list_price": "25.00", "platform": "ebay", "listing_url": ""},
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        self.assertEqual(entry.listed_at, timezone.localdate())
        self.assertEqual(entry.list_price, Decimal("25.00"))
        self.assertEqual(entry.platform, "ebay")

    def test_post_does_not_clobber_existing_listed_at(self):
        original_date = timezone.localdate() - timedelta(days=10)
        entry = _make_entry(self.user, status="holding", listed_at=original_date, platform="ebay")

        self.client.post(
            reverse("albright_reselling_app:ledger_mark_listed", args=[entry.pk]),
            {"list_price": "30.00", "platform": "mercari", "listing_url": ""},
        )

        entry.refresh_from_db()
        self.assertEqual(entry.listed_at, original_date)
        self.assertEqual(entry.list_price, Decimal("30.00"))

    def test_other_users_entry_is_not_accessible(self):
        other = User.objects.create_user(username="other", password="pw-not-real-12345")
        entry = _make_entry(other, status="holding")

        response = self.client.get(reverse("albright_reselling_app:ledger_mark_listed", args=[entry.pk]))

        self.assertEqual(response.status_code, 404)


class RecordSaleNoShippingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_no_shipping_needed_sets_shipped_at_immediately(self):
        entry = _make_entry(self.user, status="holding")

        self.client.post(reverse("albright_reselling_app:ledger_record_sale", args=[entry.pk]), {
            "sale_date": timezone.localdate().isoformat(), "sale_price": "25.00", "channel": "local_facebook",
            "selling_fees": "0", "outbound_shipping": "0", "packaging": "0", "hours_spent": "0", "notes": "",
            "no_shipping_needed": "on",
        })

        sale = entry.sales.get()
        self.assertIsNotNone(sale.shipped_at)

    def test_without_the_checkbox_shipped_at_stays_unset(self):
        entry = _make_entry(self.user, status="holding")

        self.client.post(reverse("albright_reselling_app:ledger_record_sale", args=[entry.pk]), {
            "sale_date": timezone.localdate().isoformat(), "sale_price": "25.00", "channel": "ebay",
            "selling_fees": "0", "outbound_shipping": "0", "packaging": "0", "hours_spent": "0", "notes": "",
        })

        sale = entry.sales.get()
        self.assertIsNone(sale.shipped_at)

    def test_next_param_overrides_default_redirect(self):
        entry = _make_entry(self.user, status="holding")
        board_url = reverse("albright_reselling_app:ledger_board")

        response = self.client.post(
            f"{reverse('albright_reselling_app:ledger_record_sale', args=[entry.pk])}?next={board_url}",
            {
                "sale_date": timezone.localdate().isoformat(), "sale_price": "25.00", "channel": "ebay",
                "selling_fees": "0", "outbound_shipping": "0", "packaging": "0", "hours_spent": "0", "notes": "",
                "next": board_url,
            },
        )

        self.assertRedirects(response, board_url)


class MarkShippedFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_marks_every_unshipped_sale_shipped(self):
        entry = _make_entry(self.user, status="partially_sold")
        _make_sale(entry, sale_price="10", shipped_at=None)
        _make_sale(entry, sale_price="15", shipped_at=None)

        self.client.post(reverse("albright_reselling_app:ledger_mark_shipped", args=[entry.pk]))

        self.assertEqual(entry.sales.filter(shipped_at__isnull=True).count(), 0)

    def test_get_not_allowed(self):
        entry = _make_entry(self.user, status="partially_sold")
        _make_sale(entry, shipped_at=None)

        response = self.client.get(reverse("albright_reselling_app:ledger_mark_shipped", args=[entry.pk]))

        self.assertEqual(response.status_code, 405)


# ---------------------------------------------------------------------------
# Markdown steps
# ---------------------------------------------------------------------------

class MarkdownStatusTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_not_listed_within_7_days_not_flagged(self):
        entry = _make_entry(self.user, purchase_date=timezone.localdate() - timedelta(days=3))
        status = selling.markdown_status(entry)
        self.assertFalse(status["due"])
        self.assertFalse(status["listed"])

    def test_not_listed_past_7_days_is_flagged(self):
        entry = _make_entry(self.user, purchase_date=timezone.localdate() - timedelta(days=10))
        status = selling.markdown_status(entry)
        self.assertTrue(status["due"])
        self.assertEqual(status["label"], "Not listed for 10 days")

    def test_14_day_step_due(self):
        entry = _make_entry(self.user, listed_at=timezone.localdate() - timedelta(days=14))
        status = selling.markdown_status(entry)
        self.assertTrue(status["due"])
        self.assertEqual(status["step"], 14)
        self.assertEqual(status["label"], "Send offers to watchers")

    def test_13_days_not_yet_due(self):
        entry = _make_entry(self.user, listed_at=timezone.localdate() - timedelta(days=13))
        status = selling.markdown_status(entry)
        self.assertFalse(status["due"])
        self.assertEqual(status["step"], 14)

    def test_30_day_step_due_after_14_done(self):
        entry = _make_entry(
            self.user, listed_at=timezone.localdate() - timedelta(days=31), last_markdown_step=14,
        )
        status = selling.markdown_status(entry)
        self.assertTrue(status["due"])
        self.assertEqual(status["step"], 30)
        self.assertEqual(status["label"], "Lower price about 10%")

    def test_60_day_step_due_after_30_done(self):
        entry = _make_entry(
            self.user, listed_at=timezone.localdate() - timedelta(days=61), last_markdown_step=30,
        )
        status = selling.markdown_status(entry)
        self.assertTrue(status["due"])
        self.assertEqual(status["step"], 60)
        self.assertEqual(status["label"], "Lower price about 10% again")

    def test_90_day_step_due_after_60_done(self):
        entry = _make_entry(
            self.user, listed_at=timezone.localdate() - timedelta(days=91), last_markdown_step=60,
        )
        status = selling.markdown_status(entry)
        self.assertTrue(status["due"])
        self.assertEqual(status["step"], 90)
        self.assertEqual(status["label"], "Sell locally, bundle, or donate")

    def test_no_further_step_after_90_done(self):
        entry = _make_entry(
            self.user, listed_at=timezone.localdate() - timedelta(days=200), last_markdown_step=90,
        )
        status = selling.markdown_status(entry)
        self.assertFalse(status["due"])
        self.assertIsNone(status["step"])


class MarkdownActionViewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_done_records_last_markdown_step_and_clears_it_from_due(self):
        entry = _make_entry(self.user, listed_at=timezone.localdate() - timedelta(days=14))
        self.assertTrue(selling.needs_markdown_action(entry))

        self.client.post(reverse("albright_reselling_app:ledger_markdown_done", args=[entry.pk]))

        entry.refresh_from_db()
        self.assertEqual(entry.last_markdown_step, 14)
        self.assertFalse(selling.needs_markdown_action(entry))

    def test_lower_price_updates_price_and_records_step(self):
        entry = _make_entry(
            self.user, listed_at=timezone.localdate() - timedelta(days=31), last_markdown_step=14,
            list_price=Decimal("20.00"),
        )

        self.client.post(
            f"{reverse('albright_reselling_app:ledger_edit_price', args=[entry.pk])}?step=30",
            {"list_price": "18.00", "step": "30"},
        )

        entry.refresh_from_db()
        self.assertEqual(entry.list_price, Decimal("18.00"))
        self.assertEqual(entry.last_markdown_step, 30)

    def test_plain_edit_price_does_not_touch_markdown_step(self):
        entry = _make_entry(self.user, listed_at=timezone.localdate() - timedelta(days=1), list_price=Decimal("20"))

        self.client.post(
            reverse("albright_reselling_app:ledger_edit_price", args=[entry.pk]), {"list_price": "22.00"},
        )

        entry.refresh_from_db()
        self.assertEqual(entry.list_price, Decimal("22.00"))
        self.assertEqual(entry.last_markdown_step, 0)


# ---------------------------------------------------------------------------
# Shared aging/markdown logic parity (dashboard tile <-> Ledger aging view)
# ---------------------------------------------------------------------------

class SharedAgingParityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    def test_dashboard_tile_markdown_count_matches_selling_due_flag(self):
        _make_entry(self.user, item="Due", listed_at=timezone.localdate() - timedelta(days=14))
        _make_entry(self.user, item="Not Due", listed_at=timezone.localdate() - timedelta(days=5))

        tiles = ledger_metrics.dashboard_tiles(self.user)

        self.assertEqual(tiles["markdown_count"], 1)

    def test_scorecard_aging_row_suggested_action_matches_markdown_status(self):
        entry = _make_entry(self.user, listed_at=timezone.localdate() - timedelta(days=14))

        context = ledger_metrics.scorecard_context(self.user)
        row = next(r for r in context["aging"] if r["entry"].pk == entry.pk)

        self.assertEqual(row["suggested_action"], "Send offers to watchers")

    def test_personal_items_aging_uses_same_definition(self):
        entry = _make_entry(
            self.user, ledger_type="personal", listed_at=timezone.localdate() - timedelta(days=14),
        )

        context = ledger_metrics.personal_items_context(self.user)
        row = next(r for r in context["aging"] if r["entry"].pk == entry.pk)

        self.assertEqual(row["suggested_action"], "Send offers to watchers")

    def test_not_due_item_has_no_suggested_action_in_aging_row(self):
        entry = _make_entry(self.user, listed_at=timezone.localdate() - timedelta(days=1))

        context = ledger_metrics.scorecard_context(self.user)
        row = next(r for r in context["aging"] if r["entry"].pk == entry.pk)

        self.assertIsNone(row["suggested_action"])


# ---------------------------------------------------------------------------
# Sell-down goal progress
# ---------------------------------------------------------------------------

GOAL_CFG = {"items": 2, "recover_usd": 100, "start_date": "2020-01-01"}


class SellGoalProgressTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")

    @mock.patch.dict(settings.RESELLING_SCANNER, {"SELL_GOAL": GOAL_CFG})
    def test_counts_sales_from_both_ledgers(self):
        resale_entry = _make_entry(self.user, ledger_type="resale", status="partially_sold")
        _make_sale(resale_entry, sale_price="60.00", sale_date=timezone.localdate())

        _make_entry(
            self.user, ledger_type="personal", status="sold_out", sold_for="40.00",
            sold_date=timezone.localdate(),
        )

        progress = selling.sell_goal_progress(self.user)

        self.assertEqual(progress["items_sold"], 2)
        self.assertEqual(progress["recovered"], Decimal("100.00"))
        self.assertTrue(progress["goal_met"])
        self.assertEqual(progress["items_pct"], 100)
        self.assertEqual(progress["recover_pct"], 100)

    @mock.patch.dict(settings.RESELLING_SCANNER, {"SELL_GOAL": GOAL_CFG})
    def test_sale_before_start_date_excluded(self):
        entry = _make_entry(self.user, status="sold_out", sold_for="999.00", sold_date="2019-01-01")

        progress = selling.sell_goal_progress(self.user)

        self.assertEqual(progress["items_sold"], 0)
        self.assertEqual(progress["recovered"], Decimal("0"))

    @mock.patch.dict(settings.RESELLING_SCANNER, {"SELL_GOAL": GOAL_CFG})
    def test_partial_progress_not_met(self):
        _make_entry(self.user, status="sold_out", sold_for="30.00", sold_date=timezone.localdate())

        progress = selling.sell_goal_progress(self.user)

        self.assertEqual(progress["items_sold"], 1)
        self.assertEqual(progress["recovered"], Decimal("30.00"))
        self.assertFalse(progress["goal_met"])
        self.assertEqual(progress["recover_pct"], 30)


# ---------------------------------------------------------------------------
# Today page sections
# ---------------------------------------------------------------------------

class TodaySectionsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_empty_state_shows_nothing_urgent(self):
        response = self.client.get(reverse("albright_reselling_app:today"))
        self.assertContains(response, "Nothing urgent today")
        self.assertNotContains(response, "To Ship <")
        self.assertNotContains(response, "Markdowns Due <")

    def test_to_ship_section_shown_when_present(self):
        entry = _make_entry(self.user, status="partially_sold")
        _make_sale(entry, shipped_at=None)

        response = self.client.get(reverse("albright_reselling_app:today"))

        self.assertContains(response, "To Ship")
        self.assertContains(response, entry.item)

    def test_markdowns_due_section_shown_when_present(self):
        entry = _make_entry(self.user, listed_at=timezone.localdate() - timedelta(days=14))

        response = self.client.get(reverse("albright_reselling_app:today"))

        self.assertContains(response, "Markdowns Due")
        self.assertContains(response, entry.item)

    def test_to_list_section_shows_daily_target(self):
        _make_entry(self.user, status="holding")

        response = self.client.get(reverse("albright_reselling_app:today"))

        self.assertContains(response, "List 2 today")

    @mock.patch.dict(settings.RESELLING_SCANNER, {"SELL_GOAL": GOAL_CFG})
    def test_goal_met_shows_freeze_message(self):
        _make_entry(self.user, status="sold_out", sold_for="999.00", sold_date=timezone.localdate())

        response = self.client.get(reverse("albright_reselling_app:today"))

        self.assertContains(response, "Buying freeze can lift")

    def test_todo_count_sums_time_sensitive_sections(self):
        to_ship_entry = _make_entry(self.user, item="To Ship", status="partially_sold")
        _make_sale(to_ship_entry, shipped_at=None)
        _make_entry(self.user, item="Markdown Due", listed_at=timezone.localdate() - timedelta(days=14))

        sections = selling.today_sections(self.user)

        self.assertEqual(sections["todo_count"], 2)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse("albright_reselling_app:today"))
        self.assertEqual(response.status_code, 302)


# ---------------------------------------------------------------------------
# Nav order and landing redirect
# ---------------------------------------------------------------------------

class NavAndLandingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_today_no_longer_in_reseller_nav(self):
        """Watchlist took Today's nav slot (see test_watchlist_page.py) -
        Today's page/URL stay, just unlinked from the nav."""
        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        nav_start = content.index('class="navbar__links"')
        nav_html = content[nav_start:nav_start + 2000]

        self.assertNotIn(">Today<", nav_html)
        self.assertNotIn(reverse("albright_reselling_app:today"), nav_html)

    def test_today_url_still_serves_today_page(self):
        response = self.client.get(reverse("albright_reselling_app:today"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Sell-Down Goal")

    def test_dashboard_still_reachable_at_its_own_path(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        self.assertEqual(response.status_code, 200)
