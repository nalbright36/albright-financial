"""Tests for the spot-price fetch command and read path. Never hits the real
goldapi.io API - every HTTP call is mocked."""
import os
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from albright_reselling_app.scanner.spot import get_spot
from albright_reselling_app.scanner_models import SpotPrice

BASE_SPOT_SETTINGS = {"monthly_api_limit": 90, "max_age_days": 3, "manual": {"silver": None, "gold": None}}
FETCH_TARGET = "albright_reselling_app.management.commands.fetch_spot_prices.requests.get"


def _mock_response(price):
    resp = mock.Mock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"price": price}
    return resp


@override_settings(RESELLING_SCANNER={"SPOT": BASE_SPOT_SETTINGS})
@mock.patch.dict(os.environ, {"GOLDAPI_KEY": "test-key"})
class FetchSpotPricesCommandTests(TestCase):
    @mock.patch(FETCH_TARGET)
    def test_fetch_saves_both_metals(self, mock_get):
        mock_get.side_effect = [_mock_response(30.5), _mock_response(2500.0)]
        out = StringIO()
        call_command("fetch_spot_prices", stdout=out)

        self.assertEqual(mock_get.call_count, 2)
        silver = SpotPrice.objects.get(metal="silver", source="goldapi")
        gold = SpotPrice.objects.get(metal="gold", source="goldapi")
        self.assertEqual(silver.price_usd, Decimal("30.50"))
        self.assertEqual(gold.price_usd, Decimal("2500.00"))
        self.assertIn("Saved spot", out.getvalue())

    @mock.patch(FETCH_TARGET)
    def test_second_run_same_day_makes_no_api_calls(self, mock_get):
        mock_get.side_effect = [_mock_response(30.5), _mock_response(2500.0)]
        call_command("fetch_spot_prices", stdout=StringIO())
        mock_get.reset_mock()

        call_command("fetch_spot_prices", stdout=StringIO())

        mock_get.assert_not_called()
        self.assertEqual(SpotPrice.objects.filter(metal="silver", source="goldapi").count(), 1)
        self.assertEqual(SpotPrice.objects.filter(metal="gold", source="goldapi").count(), 1)

    @mock.patch(FETCH_TARGET)
    def test_force_fetches_again(self, mock_get):
        mock_get.side_effect = [_mock_response(30.5), _mock_response(2500.0)]
        call_command("fetch_spot_prices", stdout=StringIO())
        mock_get.reset_mock()

        mock_get.side_effect = [_mock_response(31.0), _mock_response(2510.0)]
        call_command("fetch_spot_prices", "--force", stdout=StringIO())

        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(SpotPrice.objects.filter(metal="silver", source="goldapi").count(), 2)
        latest = SpotPrice.objects.filter(metal="silver", source="goldapi").order_by("-fetched_at").first()
        self.assertEqual(latest.price_usd, Decimal("31.00"))

    @override_settings(RESELLING_SCANNER={"SPOT": {**BASE_SPOT_SETTINGS, "monthly_api_limit": 1}})
    @mock.patch(FETCH_TARGET)
    def test_monthly_cap_blocks_call(self, mock_get):
        # One attempt already logged this month puts us at the cap (1).
        SpotPrice.objects.create(metal="silver", price_usd=Decimal("0"), source="goldapi_failed")

        with self.assertRaises(CommandError):
            call_command("fetch_spot_prices", stdout=StringIO(), stderr=StringIO())
        mock_get.assert_not_called()

    @mock.patch(FETCH_TARGET)
    def test_failed_call_records_failure_and_raises(self, mock_get):
        mock_get.side_effect = Exception("connection refused")

        with self.assertRaises(CommandError):
            call_command("fetch_spot_prices", stdout=StringIO(), stderr=StringIO())

        failed = SpotPrice.objects.get(metal="silver", source="goldapi_failed")
        self.assertEqual(failed.price_usd, Decimal("0"))


@override_settings(RESELLING_SCANNER={"SPOT": BASE_SPOT_SETTINGS})
class MissingApiKeyTests(TestCase):
    def test_missing_api_key_raises_without_calling_api(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(CommandError):
                call_command("fetch_spot_prices", stdout=StringIO(), stderr=StringIO())
        self.assertEqual(SpotPrice.objects.count(), 0)


@override_settings(RESELLING_SCANNER={"SPOT": BASE_SPOT_SETTINGS})
class GetSpotTests(TestCase):
    def test_returns_latest_good_price(self):
        SpotPrice.objects.create(metal="silver", price_usd=Decimal("29.00"), source="goldapi")
        SpotPrice.objects.create(metal="silver", price_usd=Decimal("30.00"), source="goldapi")
        self.assertEqual(get_spot("silver"), 30.0)

    def test_ignores_failed_rows(self):
        SpotPrice.objects.create(metal="silver", price_usd=Decimal("0"), source="goldapi_failed")
        SpotPrice.objects.create(metal="silver", price_usd=Decimal("30.00"), source="goldapi")
        self.assertEqual(get_spot("silver"), 30.0)

    def test_raises_on_stale_price_with_no_manual_fallback(self):
        stale = SpotPrice.objects.create(metal="silver", price_usd=Decimal("30.00"), source="goldapi")
        SpotPrice.objects.filter(pk=stale.pk).update(fetched_at=timezone.now() - timedelta(days=5))
        with self.assertRaises(RuntimeError):
            get_spot("silver")

    @override_settings(RESELLING_SCANNER={
        "SPOT": {**BASE_SPOT_SETTINGS, "manual": {"silver": 32.5, "gold": None}},
    })
    def test_uses_manual_price_when_stale(self):
        stale = SpotPrice.objects.create(metal="silver", price_usd=Decimal("30.00"), source="goldapi")
        SpotPrice.objects.filter(pk=stale.pk).update(fetched_at=timezone.now() - timedelta(days=5))
        self.assertEqual(get_spot("silver"), 32.5)

    def test_raises_with_no_price_at_all(self):
        with self.assertRaises(RuntimeError):
            get_spot("gold")

    @override_settings(RESELLING_SCANNER={
        "SPOT": {**BASE_SPOT_SETTINGS, "manual": {"silver": None, "gold": 2600.0}},
    })
    def test_uses_manual_price_when_no_price_at_all(self):
        self.assertEqual(get_spot("gold"), 2600.0)
