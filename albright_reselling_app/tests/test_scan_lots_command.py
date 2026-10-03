"""Tests for the scan_lots command's ScanRun bookkeeping. run_scan() itself
is mocked - this never touches ShopGoodwill."""
from datetime import timedelta
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from albright_reselling_app.scanner_models import ScanRun

RUN_SCAN_TARGET = "albright_reselling_app.management.commands.scan_lots.run_scan"

SUCCESS_RESULT = {
    "seen": 1, "candidates": [], "leads": [], "spot": {}, "failed_keywords": [],
    "keywords_scanned": [], "llm_calls": 0,
}


def _good_run(source, started_hours_ago):
    run = ScanRun.objects.create(source=source, finished_at=timezone.now(), error="")
    ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=started_hours_ago))
    return run


def _failed_run(source, started_hours_ago):
    run = ScanRun.objects.create(source=source, finished_at=timezone.now(), error="boom")
    ScanRun.objects.filter(pk=run.pk).update(started_at=timezone.now() - timedelta(hours=started_hours_ago))
    return run


class ScanLotsRunLogTests(TestCase):
    @mock.patch(RUN_SCAN_TARGET)
    def test_records_scan_run_on_success(self, mock_run_scan):
        mock_run_scan.return_value = {
            "seen": 12, "candidates": [], "leads": [], "spot": {"silver": 30.0, "gold": 2500.0},
            "failed_keywords": [], "keywords_scanned": ["morgan dollar"], "llm_calls": 0,
        }

        call_command("scan_lots", "--no-llm", stdout=StringIO())

        run = ScanRun.objects.get()
        self.assertEqual(run.source, "shopgoodwill")
        self.assertEqual(run.lots_seen, 12)
        self.assertEqual(run.candidates, 0)
        self.assertEqual(run.leads, 0)
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


class MinIntervalHoursTests(TestCase):
    @mock.patch(RUN_SCAN_TARGET)
    def test_skips_when_recent_successful_run_exists(self, mock_run_scan):
        _good_run("hibid", started_hours_ago=2)

        out = StringIO()
        call_command("scan_lots", "--source", "hibid", "--min-interval-hours", "6", "--no-llm", stdout=out)

        mock_run_scan.assert_not_called()
        self.assertIn("Skipped: last hibid scan was 2.0 hours ago (minimum 6)", out.getvalue())
        self.assertEqual(ScanRun.objects.filter(source="hibid").count(), 1)  # no new ScanRun created

    @mock.patch(RUN_SCAN_TARGET)
    def test_runs_when_last_good_run_is_older_than_interval(self, mock_run_scan):
        mock_run_scan.return_value = SUCCESS_RESULT
        _good_run("hibid", started_hours_ago=7)

        call_command("scan_lots", "--source", "hibid", "--min-interval-hours", "6", "--no-llm", stdout=StringIO())

        mock_run_scan.assert_called_once()
        self.assertEqual(ScanRun.objects.filter(source="hibid").count(), 2)  # the old one + this new one

    @mock.patch(RUN_SCAN_TARGET)
    def test_runs_when_last_run_failed_even_if_recent(self, mock_run_scan):
        """Failed runs don't count toward the interval, even if recent - a
        failure gets retried on the very next scheduled (hourly) run."""
        mock_run_scan.return_value = SUCCESS_RESULT
        _failed_run("hibid", started_hours_ago=0.2)

        call_command("scan_lots", "--source", "hibid", "--min-interval-hours", "6", "--no-llm", stdout=StringIO())

        mock_run_scan.assert_called_once()
        self.assertEqual(ScanRun.objects.filter(source="hibid").count(), 2)

    @mock.patch(RUN_SCAN_TARGET)
    def test_runs_normally_without_the_option(self, mock_run_scan):
        mock_run_scan.return_value = SUCCESS_RESULT
        _good_run("hibid", started_hours_ago=0.1)

        call_command("scan_lots", "--source", "hibid", "--no-llm", stdout=StringIO())

        mock_run_scan.assert_called_once()

    @mock.patch(RUN_SCAN_TARGET)
    def test_min_interval_scoped_to_its_own_source(self, mock_run_scan):
        """A recent good shopgoodwill run shouldn't block a hibid scan."""
        mock_run_scan.return_value = SUCCESS_RESULT
        _good_run("shopgoodwill", started_hours_ago=0.1)

        call_command("scan_lots", "--source", "hibid", "--min-interval-hours", "6", "--no-llm", stdout=StringIO())

        mock_run_scan.assert_called_once()
