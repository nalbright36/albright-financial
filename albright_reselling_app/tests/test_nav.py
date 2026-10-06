"""Tests for the reselling app's main nav: Dashboard/Ledger/Bid Calculator/
Tools at the top level, the Tools dropdown grouping AI Reviews/Auction
Scanner/Legacy Historical Data/Sleeper Segments/Insights, and every one of
those pages' URLs still resolving and loading after the regroup (URLs/
views/functionality were never touched - only the nav markup moved)."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse


class NavStructureTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="tester", password="pw-not-real-12345")
        self.client.login(username="tester", password="pw-not-real-12345")

    def test_top_level_nav_is_dashboard_ledger_calculator_tools(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        self.assertIn('class="navbar__links"', content)
        self.assertContains(response, ">Dashboard<")
        self.assertContains(response, ">Ledger<")
        self.assertContains(response, ">Bid Calculator<")
        self.assertContains(response, "Tools")

    def test_bid_calculator_moved_out_of_tools_dropdown(self):
        """Bid Calculator is now a top-level link, between Ledger and the
        Tools dropdown trigger - and no longer inside the dropdown menu
        itself. Auction Scanner/AI Reviews/Historical Data/Sleeper
        Segments/Insights are still grouped inside the dropdown."""
        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        ledger_pos = content.index(">Ledger<")
        tools_trigger_pos = content.index("navbar__dropdown-trigger")
        between = content[ledger_pos:tools_trigger_pos]

        self.assertIn(">Bid Calculator<", between)
        self.assertEqual(between.count("<a href"), 1)  # only the Bid Calculator link lives here now

        dropdown_start = content.index('id="tools-dropdown-menu"')
        dropdown_end = content.index("</div>", dropdown_start)
        dropdown_html = content[dropdown_start:dropdown_end]
        self.assertNotIn(reverse("albright_reselling_app:calculator"), dropdown_html)

    def test_tools_dropdown_contains_every_expected_item(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, 'id="tools-dropdown-menu"')
        self.assertContains(response, reverse("albright_reselling_app:ai_review_history"))
        self.assertContains(response, reverse("albright_reselling_app:auction_scanner"))
        self.assertContains(response, reverse("albright_reselling_app:historical_data"))
        self.assertContains(response, reverse("albright_reselling_app:sleeper_segments"))
        self.assertContains(response, reverse("albright_reselling_app:insights"))

    def test_historical_data_relabeled_in_nav(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, "Legacy Historical Data")
        self.assertNotContains(response, ">Historical Data<")

    def test_dropdown_trigger_is_keyboard_focusable_button(self):
        """A real <button> (not a <span>), so Tab reaches it and a native
        click fires on Enter/Space - the actual JS toggle is exercised in
        a browser, not here, but the markup prerequisite is checkable."""
        response = self.client.get(reverse("albright_reselling_app:dashboard"))
        content = response.content.decode()

        self.assertIn('<button type="button" class="navbar__dropdown-trigger"', content)
        self.assertIn('aria-haspopup="true"', content)
        self.assertIn('aria-controls="tools-dropdown-menu"', content)

    def test_dashboard_view_all_reviews_link_still_works(self):
        response = self.client.get(reverse("albright_reselling_app:dashboard"))

        self.assertContains(response, reverse("albright_reselling_app:ai_review_history"))

    def test_every_tools_page_still_loads(self):
        for url_name in (
            "auction_scanner", "ai_review_history", "historical_data", "sleeper_segments", "insights", "calculator",
        ):
            response = self.client.get(reverse(f"albright_reselling_app:{url_name}"))
            self.assertEqual(response.status_code, 200, f"{url_name} did not load")
