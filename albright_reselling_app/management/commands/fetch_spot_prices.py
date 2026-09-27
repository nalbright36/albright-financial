"""Usage:
    python manage.py fetch_spot_prices             # fetch silver + gold, skip metals already fetched today
    python manage.py fetch_spot_prices --force     # fetch even if today's price is already stored

Run this once a day as a PythonAnywhere scheduled task. This is the ONLY
place that calls goldapi.io - the scanner (scanner/spot.py) only reads
whatever this command last stored in SpotPrice.

The free goldapi plan allows 100 requests/month and each metal costs one
request (silver + gold = 2/day), so a daily run uses ~60/month. Safeguards:
  - skip a metal if we already have today's price for it (unless --force)
  - a hard monthly cap on API attempts, counted from the database
  - a failed call is recorded too (source "goldapi_failed", price 0) so it
    still counts toward the cap and shows up in admin
"""
import logging
import os
from decimal import Decimal

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from albright_reselling_app.scanner_models import SpotPrice

log = logging.getLogger(__name__)

SYMBOLS = {"silver": "XAG", "gold": "XAU"}
OK, FAILED = "goldapi", "goldapi_failed"


def _cfg():
    return settings.RESELLING_SCANNER["SPOT"]


def _fetch_goldapi(metal: str, key: str) -> float:
    resp = requests.get(f"https://www.goldapi.io/api/{SYMBOLS[metal]}/USD",
                         headers={"x-access-token": key}, timeout=15)
    resp.raise_for_status()
    return float(resp.json()["price"])


def _api_attempts_this_month() -> int:
    start = timezone.localdate().replace(day=1)
    return SpotPrice.objects.filter(source__in=[OK, FAILED], fetched_at__date__gte=start).count()


def _has_today_price(metal: str) -> bool:
    latest = SpotPrice.objects.filter(metal=metal, source=OK).first()
    return bool(latest and timezone.localtime(latest.fetched_at).date() == timezone.localdate())


class Command(BaseCommand):
    help = "Fetch today's silver/gold spot prices from goldapi.io and store them. Run once a day."

    def add_arguments(self, parser):
        parser.add_argument("--force", action="store_true", help="Fetch even if today's price is already stored")

    def handle(self, *args, **opts):
        key = os.environ.get("GOLDAPI_KEY")
        if not key:
            raise CommandError("GOLDAPI_KEY is not set - refusing to call the API")

        limit = _cfg()["monthly_api_limit"]
        force = opts["force"]
        saved = {}

        for metal in SYMBOLS:
            if not force and _has_today_price(metal):
                latest = SpotPrice.objects.filter(metal=metal, source=OK).first()
                saved[metal] = float(latest.price_usd)
                self.stdout.write(f"{metal}: already have today's price (${saved[metal]:,.2f}), skipping "
                                   f"(use --force to refetch)")
                continue

            attempts = _api_attempts_this_month()
            if attempts >= limit:
                raise CommandError(f"Monthly goldapi limit reached ({attempts}/{limit}) - refusing to call "
                                    f"the API for {metal}")

            try:
                price = _fetch_goldapi(metal, key)
            except Exception as exc:  # noqa: BLE001 - any failure is recorded, then reported and raised
                SpotPrice.objects.create(metal=metal, price_usd=Decimal("0"), source=FAILED)
                log.warning("Spot fetch failed for %s: %s", metal, exc)
                raise CommandError(f"Fetch failed for {metal}: {exc}")

            SpotPrice.objects.create(metal=metal, price_usd=Decimal(str(round(price, 2))), source=OK)
            saved[metal] = price

        attempts = _api_attempts_this_month()
        summary = ", ".join(f"{metal} ${price:,.2f}" for metal, price in saved.items())
        self.stdout.write(self.style.SUCCESS(f"Saved spot: {summary} (API calls this month: {attempts}/{limit})"))
