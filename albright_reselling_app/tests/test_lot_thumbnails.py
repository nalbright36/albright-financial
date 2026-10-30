"""Every lot table renders the shared photo thumbnail (_lot_thumb.html) as
its first column, from the lot's own image_url - dashboard: Candidates
ending soon, Dismissed, Ending later, Closest calls, MaxSold by estate
(the estate's top candidate's photo), Results; Watchlist page: Watching
and Archive. (Check by hand and Leads are covered in
test_review_queue.py.) Also checks the row's other controls and the
expandable detail row survive the extra column."""
import re
from datetime import timedelta
from decimal import Decimal

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner_models import BidWatch, LotDismissal, LotEvaluation, SourcedLot
from albright_labs.testing import with_apps


def _lot(external_id, end_in=timedelta(hours=3), source="shopgoodwill", image=True, raw=None, **extra):
    return SourcedLot.objects.create(
        source=source, external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Thumb Lot {external_id}", current_price=Decimal("10.00"), end_time=timezone.now() + end_in,
        image_url=f"https://img.example.com/{external_id}.jpg" if image else "", raw=raw or {}, **extra,
    )


def _evaluate(lot, is_candidate=True, max_bid="30.00", headroom="20.00", confidence="high"):
    return LotEvaluation.objects.create(
        lot=lot, category="coins", is_candidate=is_candidate, max_bid=Decimal(max_bid),
        headroom=Decimal(headroom), confidence=confidence, expected_sale=Decimal("50.00"),
    )


def _table(content, table_id):
    return content.split(f'id="{table_id}"')[1].split("</table>")[0]


def _first_cell(table, lot_pk=None):
    """The first <td> of this lot's row (or of the first data row)."""
    if lot_pk is not None:
        row = re.search(rf'<tr class="scanner-row" data-lot-id="{lot_pk}".*?</tr>', table, re.S).group(0)
    else:
        row = re.search(r"<tbody>\s*<tr[^>]*>.*?</tr>", table, re.S).group(0)
    return re.search(r"<td[^>]*>.*?</td>", row, re.S).group(0)


class ThumbnailAssertions(TestCase):
    def setUp(self):
        self.user = with_apps(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        self.client.login(username="tester", password="pw-not-real-12345")

    def assert_thumb(self, cell, image_url):
        self.assertIn('class="lot-thumb-cell"', cell)
        self.assertIn(f'src="{image_url}"', cell)
        self.assertIn(f'data-full-src="{image_url}"', cell)
        self.assertIn('loading="lazy"', cell)
        self.assertIn("js-lightbox", cell)

    def assert_header_starts_with_photo_then_title(self, table):
        headers = re.findall(r"<th(?:\s[^>]*)?>", table.split("</thead>")[0])  # <th>, never <thead>
        self.assertIn("lot-thumb-col", headers[0])
        rest = [h for h in headers[1:] if "row-toggle-col" not in h]  # chevron column, where present
        self.assertTrue("lot-title-col" in rest[0] or 'data-sort-key="auction"' in rest[0], rest[0])

    def dashboard(self):
        return self.client.get(reverse("albright_reselling_app:dashboard")).content.decode()


class DashboardThumbnailTests(ThumbnailAssertions):
    def test_candidates_ending_soon(self):
        lot = _lot("soon")
        _evaluate(lot)

        table = _table(self.dashboard(), "table-candidates-soon")

        self.assert_header_starts_with_photo_then_title(table)
        self.assert_thumb(_first_cell(table, lot.pk), lot.image_url)
        # The row's other controls and its detail row are all still there.
        row = re.search(rf'<tr class="scanner-row" data-lot-id="{lot.pk}".*?</tr>', table, re.S).group(0)
        for control in ("js-calc-from-lot", "js-watch-toggle", "js-dismiss-btn", 'class="row-toggle"'):
            self.assertIn(control, row)
        self.assertIn('data-headroom="20.00"', row)  # sort/filter attributes unchanged
        detail = table.split(row)[1]
        self.assertTrue(detail.lstrip().startswith('<tr class="scanner-detail" hidden>'))
        self.assertIn('colspan="10"', detail.split("</tr>")[0])

    def test_dismissed(self):
        lot = _lot("dismissed")
        _evaluate(lot)
        LotDismissal.objects.create(lot=lot, dismissed_at=timezone.now(), headroom_at_dismissal=Decimal("20.00"))

        table = _table(self.dashboard(), "table-dismissed")

        self.assert_thumb(_first_cell(table), lot.image_url)

    def test_ending_later(self):
        lot = _lot("later", end_in=timedelta(days=2))
        _evaluate(lot)

        table = _table(self.dashboard(), "table-candidates-later")

        self.assert_header_starts_with_photo_then_title(table)
        self.assert_thumb(_first_cell(table), lot.image_url)
        self.assertIn('colspan="10"', table)

    def test_closest_calls(self):
        lot = _lot("closest")
        _evaluate(lot, is_candidate=False, headroom="-5.00")

        table = _table(self.dashboard(), "table-closest-calls")

        self.assert_header_starts_with_photo_then_title(table)
        self.assert_thumb(_first_cell(table), lot.image_url)

    def test_maxsold_estate_uses_its_top_candidates_photo(self):
        pickup = {"_pickup": {"auction_id": 77, "auction_title": "Smith Estate", "city": "Austin"}}
        small = _lot("estate-small", source="maxsold", raw=pickup)
        _evaluate(small, headroom="5.00")
        top = _lot("estate-top", source="maxsold", raw=pickup)
        _evaluate(top, headroom="40.00")

        table = _table(self.dashboard(), "table-maxsold-estates")

        self.assert_header_starts_with_photo_then_title(table)
        cell = _first_cell(table)
        self.assert_thumb(cell, top.image_url)
        self.assertNotIn(small.image_url, cell)

    def test_results(self):
        lot = _lot("result", end_in=-timedelta(days=1), final_price=Decimal("15.00"), is_closed=True)
        _evaluate(lot, is_candidate=False)

        table = _table(self.dashboard(), "table-results")

        self.assert_header_starts_with_photo_then_title(table)
        self.assert_thumb(_first_cell(table), lot.image_url)

    def test_placeholder_when_no_image(self):
        lot = _lot("no-photo", image=False)
        _evaluate(lot)

        cell = _first_cell(_table(self.dashboard(), "table-candidates-soon"), lot.pk)

        self.assertIn('class="lot-thumb lot-thumb--empty"', cell)
        self.assertNotIn("<img", cell)


class WatchlistPageThumbnailTests(ThumbnailAssertions):
    def page(self):
        return self.client.get(reverse("albright_reselling_app:watchlist")).content.decode()

    def test_watching_table(self):
        lot = _lot("watching")
        BidWatch.objects.create(lot=lot, status="watching", my_max_bid=Decimal("25.00"))

        content = self.page()
        table = _table(content, "table-watchlist")

        self.assert_header_starts_with_photo_then_title(table)
        cell = _first_cell(table, lot.pk)
        self.assert_thumb(cell, lot.image_url)
        row = re.search(rf'<tr class="scanner-row" data-lot-id="{lot.pk}".*?</tr>', table, re.S).group(0)
        for control in ("js-calc-from-lot", "js-watch-remove"):
            self.assertIn(control, row)
        self.assertIn('<dialog id="lightbox"', content)
        self.assertIn("js/lightbox.js", content)

    def test_archive_table(self):
        lot = _lot("archived", end_in=-timedelta(days=1), final_price=Decimal("20.00"), is_closed=True)
        BidWatch.objects.create(
            lot=lot, status="unknown", archived_at=timezone.now(), archive_reason="soft_removed",
            compare_max=Decimal("25.00"), compare_source="manual", final_price=Decimal("20.00"), outcome="below",
        )

        table = _table(self.page(), "table-archive")

        self.assert_header_starts_with_photo_then_title(table)
        self.assert_thumb(_first_cell(table, lot.pk), lot.image_url)
