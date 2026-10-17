"""Tests for the dedicated Watchlist page (scanner_views.watchlist_page,
watchlist.html) and what moved around it: the nav's Watchlist item (in
Today's old slot) with its count badge (red when any lot is over My bid -
albright_reselling_app.context_processors.watchlist_nav), the page's live
table and Recently closed section, the dashboard no longer rendering a
Watchlist section, and the reseller root now landing on the Dashboard
while the Today page keeps working at its own URL."""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner.dashboard import recently_closed_watches, watchlist_nav_summary
from albright_reselling_app.scanner_models import BidWatch, LotEvaluation, SourcedLot


def _make_lot(external_id, current_price="10.00", end_in=timedelta(hours=5), final_price=None, source="shopgoodwill"):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Watch Page Lot {external_id}", current_price=Decimal(current_price),
        end_time=timezone.now() + end_in, raw={},
        final_price=Decimal(final_price) if final_price is not None else None, is_closed=final_price is not None,
    )


def _watch(lot, my_max_bid=None, status="watching"):
    return BidWatch.objects.create(
        lot=lot, status=status, my_max_bid=Decimal(my_max_bid) if my_max_bid is not None else None,
    )


def _evaluate(lot, max_bid="30.00", category="coins"):
    return LotEvaluation.objects.create(lot=lot, category=category, max_bid=Decimal(max_bid))


def _nav_html(content):
    start = content.index('class="navbar__links"')
    return content[start:start + 3000]


def _row_html(content, table_id, lot):
    table = content.split(f'id="{table_id}"')[1].split("</table>")[0]
    match = re.search(rf'<tr class="scanner-row" data-lot-id="{lot.pk}".*?</tr>', table, re.S)
    assert match, f"no {table_id} row for lot {lot.pk}"
    return match.group(0)


class LoggedInTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def get(self, name):
        return self.client.get(reverse(f"albright_reselling_app:{name}"))


class NavTests(LoggedInTestCase):
    def test_watchlist_replaces_today_in_the_same_position(self):
        nav = _nav_html(self.get("dashboard").content.decode())

        self.assertIn(f'href="{reverse("albright_reselling_app:watchlist")}"', nav)
        self.assertNotIn(">Today<", nav)
        self.assertNotIn(reverse("albright_reselling_app:today"), nav)
        # First reseller link, ahead of Dashboard - where Today used to be.
        self.assertLess(nav.index('id="nav-watchlist-link"'), nav.index(">Dashboard<"))

    def test_badge_counts_open_watched_lots(self):
        _watch(_make_lot("nav-1"), my_max_bid="25.00")
        _watch(_make_lot("nav-2"), my_max_bid="25.00")
        _watch(_make_lot("nav-closed", end_in=-timedelta(hours=1)), my_max_bid="25.00")  # closed: not counted
        _watch(_make_lot("nav-lost", end_in=-timedelta(days=1)), my_max_bid="5.00", status="lost")

        nav = _nav_html(self.get("dashboard").content.decode())

        badge = re.search(r'<span\s+id="nav-watchlist-badge" class="badge (\w+)"[^>]*>(\d+)</span>', nav)
        self.assertIsNotNone(badge)
        self.assertEqual(badge.group(2), "2")
        self.assertEqual(badge.group(1), "neutral")

    def test_badge_red_when_any_current_bid_above_my_bid(self):
        _watch(_make_lot("nav-3", current_price="10.00"), my_max_bid="25.00")
        _watch(_make_lot("nav-4", current_price="30.00"), my_max_bid="25.00")  # over

        nav = _nav_html(self.get("dashboard").content.decode())

        self.assertRegex(nav, r'id="nav-watchlist-badge" class="badge loss"[^>]*>2</span>')

    def test_badge_red_uses_scanner_max_fallback(self):
        lot = _make_lot("nav-5", current_price="40.00")
        _evaluate(lot, max_bid="30.00")
        _watch(lot)  # no max of its own -> My bid is the scanner's $30

        self.assertEqual(watchlist_nav_summary(), {"count": 1, "any_over": True})

    def test_badge_not_red_when_bid_equals_my_bid(self):
        _watch(_make_lot("nav-6", current_price="25.00"), my_max_bid="25.00")

        self.assertEqual(watchlist_nav_summary(), {"count": 1, "any_over": False})

    def test_badge_hidden_when_nothing_watched(self):
        nav = _nav_html(self.get("dashboard").content.decode())

        self.assertRegex(nav, r'id="nav-watchlist-badge"[^>]*hidden>0</span>')

    def test_badge_on_every_reseller_page(self):
        _watch(_make_lot("nav-7"), my_max_bid="25.00")
        for name in ("watchlist", "ledger_resale", "calculator", "insights"):
            with self.subTest(page=name):
                self.assertRegex(
                    _nav_html(self.get(name).content.decode()), r'id="nav-watchlist-badge"[^>]*>1</span>',
                )

    def test_summary_endpoint_matches_badge(self):
        _watch(_make_lot("nav-8", current_price="30.00"), my_max_bid="25.00")

        self.assertEqual(self.get("watchlist_summary").json(), {"count": 1, "any_over": True})


class WatchlistPageTests(LoggedInTestCase):
    def test_lists_watched_lots_sorted_by_end_time(self):
        later = _make_lot("page-later", end_in=timedelta(hours=8))
        sooner = _make_lot("page-sooner", end_in=timedelta(hours=2))
        _watch(later, my_max_bid="25.00")
        _watch(sooner, my_max_bid="25.00")

        content = self.get("watchlist").content.decode()

        self.assertEqual(self.get("watchlist").status_code, 200)
        self.assertLess(content.index(sooner.title), content.index(later.title))
        for header in ("Title", "Source", "Category", "Current Bid", "My bid", "Headroom", "Ends In", "Actions"):
            self.assertIn(f">{header}</th>", content)

    def test_row_has_calc_and_remove_and_page_has_dialog(self):
        lot = _make_lot("page-actions")
        _watch(lot, my_max_bid="25.00")

        content = self.get("watchlist").content.decode()
        row = _row_html(content, "table-watchlist", lot)

        self.assertIn("js-calc-from-lot", row)
        self.assertIn("js-watch-remove", row)
        self.assertIn('<dialog id="calc-dialog"', content)

    def test_headroom_is_my_bid_minus_current_and_red_only_when_over(self):
        under = _make_lot("page-under", current_price="10.00")
        over = _make_lot("page-over", current_price="30.00")
        _watch(under, my_max_bid="25.00")
        _watch(over, my_max_bid="25.00")

        content = self.get("watchlist").content.decode()
        under_row = _row_html(content, "table-watchlist", under)
        over_row = _row_html(content, "table-watchlist", over)

        self.assertIn('class="js-headroom" style="color: var(--gain);">$15.00<', under_row)
        self.assertIn('class="js-my-bid" style="color: var(--gain);"', under_row)
        self.assertIn('data-over="0"', under_row)
        self.assertIn('class="js-headroom" style="color: var(--loss);">$-5.00<', over_row)
        self.assertIn('class="js-my-bid" style="color: var(--loss);"', over_row)
        self.assertIn('data-over="1"', over_row)

    def test_filters_render_for_sources_and_categories_present(self):
        coins = _make_lot("page-f1", source="shopgoodwill")
        _evaluate(coins, category="coins")
        jewelry = _make_lot("page-f2", source="maxsold")
        _evaluate(jewelry, category="jewelry")
        _watch(coins, my_max_bid="25.00")
        _watch(jewelry, my_max_bid="25.00")

        content = self.get("watchlist").content.decode()

        self.assertIn('id="wl-source"', content)
        self.assertIn('<option value="maxsold">MaxSold</option>', content)
        self.assertIn('<option value="shopgoodwill">ShopGoodwill</option>', content)
        self.assertIn('<option value="jewelry">Jewelry</option>', content)
        self.assertIn('id="wl-over-only"', content)
        self.assertIn("Only show over my bid", content)
        self.assertIn("js/watchlist.js", content)

    def test_empty_state(self):
        response = self.get("watchlist")

        self.assertContains(response, "Nothing on your watchlist. Use &#9734; Add to Watchlist on any dashboard lot.")
        self.assertNotContains(response, 'id="wl-source"')

    def test_closed_lot_not_in_live_table(self):
        lot = _make_lot("page-closed", end_in=-timedelta(hours=1))
        _watch(lot, my_max_bid="25.00")

        content = self.get("watchlist").content.decode()

        self.assertNotIn('id="table-watchlist"', content)
        self.assertIn(lot.title, content.split('id="recently-closed"')[1])

    def test_requires_login(self):
        self.client.logout()

        self.assertEqual(self.get("watchlist").status_code, 302)


class RecentlyClosedTests(LoggedInTestCase):
    def test_likely_won_lost_and_window(self):
        won = _make_lot("rc-won", end_in=-timedelta(days=1), final_price="20.00")
        lost = _make_lot("rc-lost", end_in=-timedelta(days=2), final_price="40.00")
        old = _make_lot("rc-old", end_in=-timedelta(days=8), final_price="20.00")
        _watch(won, my_max_bid="25.00", status="likely_won")
        _watch(lost, my_max_bid="25.00", status="lost")
        _watch(old, my_max_bid="25.00", status="likely_won")

        content = self.get("watchlist").content.decode()
        section = content.split('id="recently-closed"')[1]

        self.assertNotIn(old.title, section)
        won_row = _row_html(content, "table-recently-closed", won)
        lost_row = _row_html(content, "table-recently-closed", lost)
        self.assertIn("$20.00", won_row)          # final price
        self.assertIn("$25.00", won_row)          # My bid
        self.assertIn(">Likely won<", won_row)
        self.assertIn(reverse("albright_reselling_app:ledger_win_lot", args=[won.pk]), won_row)
        self.assertIn(">Lost<", lost_row)
        self.assertNotIn(reverse("albright_reselling_app:ledger_win_lot", args=[lost.pk]), lost_row)

    def test_most_recently_ended_first(self):
        earlier = _make_lot("rc-earlier", end_in=-timedelta(days=3), final_price="20.00")
        latest = _make_lot("rc-latest", end_in=-timedelta(hours=2), final_price="20.00")
        _watch(earlier, my_max_bid="25.00")
        _watch(latest, my_max_bid="25.00")

        rows = recently_closed_watches(timezone.now())

        self.assertEqual([r["lot_id"] for r in rows], [latest.pk, earlier.pk])

    def test_outcome_uses_scanner_max_when_no_max_set(self):
        lot = _make_lot("rc-scanner", end_in=-timedelta(hours=3), final_price="20.00")
        _evaluate(lot, max_bid="30.00")
        _watch(lot, status="unknown")

        row = recently_closed_watches(timezone.now())[0]

        self.assertEqual(row["my_bid"], Decimal("30.00"))
        self.assertEqual(row["my_bid_source"], "scanner")
        self.assertEqual(row["outcome"], "likely_won")

    def test_pending_when_no_final_price_yet(self):
        lot = _make_lot("rc-pending", end_in=-timedelta(hours=1))
        _watch(lot, my_max_bid="25.00")

        row = recently_closed_watches(timezone.now())[0]

        self.assertEqual(row["outcome"], "pending")
        self.assertEqual(row["outcome_display"], "Pending")

    def test_section_is_collapsible(self):
        content = self.get("watchlist").content.decode()
        section = content.split('id="recently-closed"')[1]

        self.assertIn('js-collapsible-header" data-collapse-key="watchlist-recently-closed"', section)
        self.assertIn('class="js-collapsible-body"', section)
        self.assertIn("No watched lots closed in the last 7 days.", section)


class DashboardAndRedirectTests(LoggedInTestCase):
    def test_dashboard_has_no_watchlist_section_and_still_excludes_watched_lots(self):
        lot = _make_lot("dash-1")
        LotEvaluation.objects.create(
            lot=lot, category="coins", is_candidate=True, max_bid=Decimal("30.00"), headroom=Decimal("20.00"),
            confidence="high",
        )
        _watch(lot, my_max_bid="25.00")

        content = self.get("dashboard").content.decode()
        body = content[content.index("</nav>"):]  # past the navbar

        self.assertNotIn('id="table-watchlist"', body)
        self.assertNotIn('id="watchlist-body"', body)
        self.assertNotIn(lot.title, body)

    def test_reseller_root_redirects_to_dashboard(self):
        response = self.client.get("/albright_reselling_app/")

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))

    def test_remembered_app_redirect_goes_to_dashboard(self):
        self.client.cookies["last_app"] = "reseller"

        response = self.client.get("/")

        self.assertRedirects(response, reverse("albright_reselling_app:dashboard"))

    def test_today_url_still_loads(self):
        response = self.get("today")

        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "today.html")
