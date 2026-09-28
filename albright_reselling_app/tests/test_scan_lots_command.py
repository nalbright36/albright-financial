"""Tests for the scan_lots command's ScanRun bookkeeping. run_scan() itself
is mocked - this never touches ShopGoodwill."""
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase

from albright_reselling_app.scanner_models import ScanRun

RUN_SCAN_TARGET = "albright_reselling_app.management.commands.scan_lots.run_scan"


class ScanLotsRunLogTests(TestCase):
    @mock.patch(RUN_SCAN_TARGET)
    def test_records_scan_run_on_success(self, mock_run_scan):
        mock_run_scan.return_value = {
            "seen": 12, "candidates": [], "spot": {"silver": 30.0, "gold": 2500.0},
            "failed_keywords": [], "llm_calls": 0,
        }

        call_command("scan_lots", "--no-llm", stdout=StringIO())

        run = ScanRun.objects.get()
        self.assertEqual(run.source, "shopgoodwill")
        self.assertEqual(run.lots_seen, 12)
        self.assertEqual(run.candidates, 0)
        self.assertEqual(run.failed_keywords, [])
        self.assertFalse(run.stopped_early)
        self.assertEqual(run.error, "")
        self.assertIsNotNone(run.finished_at)

    @mock.patch(RUN_SCAN_TARGET)
    def test_records_error_on_failure_and_reraises(self, mock_run_scan):
        mock_run_scan.side_effect = RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            call_command("scan_lots", "--no-llm", stdout=StringIO())

        run = ScanRun.objects.get()
        self.assertEqual(run.source, "shopgoodwill")
        self.assertEqual(run.error, "boom")
        self.assertIsNotNone(run.finished_at)
        # nothing from a successful result should have been filled in
        self.assertEqual(run.lots_seen, 0)
