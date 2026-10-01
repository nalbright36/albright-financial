"""Tests for the export_features management command. No network - just DB
fixtures and a CSV written to a temp file, read back and checked."""
import csv
import os
import tempfile
from datetime import timedelta
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from albright_reselling_app.scanner_models import LotEvaluation, SourcedLot


def _make_lot(external_id, is_closed, final_price=None, max_bid="20.00", melt_value="18.00",
              expected_sale="19.00", category="coins", end_time=None, features=None, bid_count_at_close=None):
    lot = SourcedLot.objects.create(
        source="shopgoodwill", external_id=external_id, url=f"https://example.com/{external_id}",
        title=f"Morgan Silver Dollar {external_id}", current_price=Decimal("10.00"),
        end_time=end_time or (timezone.now() - timedelta(hours=2)),
        is_closed=is_closed, final_price=Decimal(str(final_price)) if final_price is not None else None,
        features=features or {}, bid_count_at_close=bid_count_at_close,
    )
    LotEvaluation.objects.create(
        lot=lot, category=category, max_bid=Decimal(max_bid), melt_value=Decimal(melt_value),
        expected_sale=Decimal(expected_sale),
    )
    return lot


class ExportFeaturesTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        self.tmp.close()
        self.addCleanup(lambda: os.path.exists(self.tmp.name) and os.remove(self.tmp.name))

    def _run(self, *args):
        out = StringIO()
        call_command("export_features", "--out", self.tmp.name, *args, stdout=out)
        with open(self.tmp.name, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f)), out.getvalue()

    def test_only_closed_lots_are_exported(self):
        _make_lot("closed-1", is_closed=True, final_price="15.00")
        _make_lot("open-1", is_closed=False)

        rows, _ = self._run()

        titles = [r["title"] for r in rows]
        self.assertIn("Morgan Silver Dollar closed-1", titles)
        self.assertNotIn("Morgan Silver Dollar open-1", titles)

    def test_core_columns_populated(self):
        _make_lot("core-1", is_closed=True, final_price="15.00", max_bid="20.00", melt_value="18.00",
                  expected_sale="19.00", bid_count_at_close=4)

        rows, _ = self._run()

        row = rows[0]
        self.assertEqual(row["source"], "shopgoodwill")
        self.assertEqual(row["category"], "coins")
        self.assertEqual(row["final_price"], "15.00")
        self.assertEqual(row["melt_value"], "18.00")
        self.assertEqual(row["expected_sale"], "19.00")
        self.assertEqual(row["max_bid"], "20.00")
        self.assertEqual(row["final_to_melt_ratio"], str(round(15 / 18, 4)))
        self.assertEqual(row["final_to_expected_ratio"], str(round(15 / 19, 4)))
        self.assertEqual(row["bid_count_at_close"], "4")

    def test_closed_under_max_bool(self):
        _make_lot("under-1", is_closed=True, final_price="15.00", max_bid="20.00")
        _make_lot("over-1", is_closed=True, final_price="25.00", max_bid="20.00")

        rows, _ = self._run()

        by_id = {r["title"]: r for r in rows}
        self.assertEqual(by_id["Morgan Silver Dollar under-1"]["closed_under_max"], "True")
        self.assertEqual(by_id["Morgan Silver Dollar over-1"]["closed_under_max"], "False")

    def test_every_features_key_becomes_a_column(self):
        _make_lot("featkeys-1", is_closed=True, final_price="15.00",
                  features={"title_length": 20, "photo_count": 3, "at_close_category": "coins"})
        _make_lot("featkeys-2", is_closed=True, final_price="16.00",
                  features={"title_length": 8})  # a different, smaller feature set

        rows, _ = self._run()

        by_id = {r["title"]: r for r in rows}
        row1 = by_id["Morgan Silver Dollar featkeys-1"]
        row2 = by_id["Morgan Silver Dollar featkeys-2"]
        self.assertEqual(row1["title_length"], "20")
        self.assertEqual(row1["photo_count"], "3")
        self.assertEqual(row1["at_close_category"], "coins")
        self.assertEqual(row2["title_length"], "8")
        self.assertEqual(row2["photo_count"], "")  # missing key -> blank, not an error

    def test_since_filters_by_end_time(self):
        _make_lot("old-1", is_closed=True, final_price="15.00",
                  end_time=timezone.now() - timedelta(days=30))
        _make_lot("recent-1", is_closed=True, final_price="15.00",
                  end_time=timezone.now() - timedelta(days=1))

        since = (timezone.now() - timedelta(days=5)).strftime("%Y-%m-%d")
        rows, _ = self._run("--since", since)

        titles = [r["title"] for r in rows]
        self.assertNotIn("Morgan Silver Dollar old-1", titles)
        self.assertIn("Morgan Silver Dollar recent-1", titles)

    def test_reports_row_count(self):
        _make_lot("count-1", is_closed=True, final_price="15.00")
        _make_lot("count-2", is_closed=True, final_price="16.00")

        _, output = self._run()

        self.assertIn("Wrote 2 closed lot(s)", output)
