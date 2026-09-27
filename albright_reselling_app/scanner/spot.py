"""Spot prices, read-only from the database.

The API call itself lives in the `fetch_spot_prices` management command, run
once a day as a scheduled task (see that command's docstring for details).
This module never calls goldapi.io - it only reads whatever that command
last stored, and refuses to hand back a stale price rather than let a scan
silently compute a max bid off outdated market data.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from ..scanner_models import SpotPrice

log = logging.getLogger(__name__)
SYMBOLS = {"silver": "XAG", "gold": "XAU"}
FAILED = "goldapi_failed"


def _cfg():
    return settings.RESELLING_SCANNER["SPOT"]


def get_spot(metal: str) -> float:
    cfg = _cfg()
    latest = SpotPrice.objects.filter(metal=metal).exclude(source=FAILED).first()

    if latest:
        age = timezone.now() - latest.fetched_at
        if age <= timedelta(days=cfg["max_age_days"]):
            return float(latest.price_usd)
        manual = cfg.get("manual", {}).get(metal)
        if manual:
            return float(manual)
        raise RuntimeError(
            f"{metal} spot price is stale (last fetched {latest.fetched_at}, older than "
            f"{cfg['max_age_days']} day(s)) - check the fetch_spot_prices scheduled task"
        )

    manual = cfg.get("manual", {}).get(metal)
    if manual:
        return float(manual)
    raise RuntimeError(f"No {metal} spot price available - run `python manage.py fetch_spot_prices`")


def get_all_spot() -> dict:
    return {metal: get_spot(metal) for metal in SYMBOLS}
