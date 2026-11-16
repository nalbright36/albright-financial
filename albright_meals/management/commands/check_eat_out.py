"""manage.py check_eat_out: one live Eat out search, to see why the page says
"unavailable" (for example on PythonAnywhere). Skips the cache and tries every
Overpass host in eatout.OVERPASS_URLS, printing what each one did.

    python manage.py check_eat_out
    python manage.py check_eat_out --lat 39.53 --lon -104.87 --far 30
"""
import time

import requests
from django.core.management.base import BaseCommand

from albright_meals import eatout


class Command(BaseCommand):
    help = "Run one test Eat out (Overpass) search and print the result or the exact error."

    def add_arguments(self, parser):
        parser.add_argument("--lat", type=float, default=39.5300, help="Latitude (default: Lone Tree, CO)")
        parser.add_argument("--lon", type=float, default=-104.8710, help="Longitude")
        parser.add_argument("--far", choices=list(eatout.RADIUS_MILES), default="15",
                            help="walk (1 mi), 15 (5 mi) or 30 (12 mi)")

    def handle(self, *args, lat, lon, far, **options):
        out = self.stdout.write
        out("API key present: not needed (OpenStreetMap Overpass has no key)")
        out(f"Search: {lat:.4f}, {lon:.4f}, radius {eatout.RADIUS_MILES[far]:g} mi "
            f"({eatout.fetch_meters(far)} m)")
        query = eatout._query(lat, lon, far)
        any_ok = False
        for url in eatout.overpass_urls():
            started = time.monotonic()
            try:
                response = requests.post(url, data={"data": query}, timeout=eatout.TIMEOUT,
                                         headers={"Accept": "application/json", "User-Agent": eatout.USER_AGENT})
            except requests.RequestException as exc:
                out(self.style.ERROR(f"{eatout._host(url)}: HTTP status: none - {type(exc).__name__}: {exc}"))
                continue
            seconds = time.monotonic() - started
            line = f"{eatout._host(url)}: HTTP status {response.status_code} in {seconds:.1f}s"
            if response.status_code >= 400:
                out(self.style.ERROR(f"{line} - error: {eatout._provider_message(response) or '(no message)'}"))
                continue
            try:
                payload = response.json()
                elements = payload["elements"]
            except (ValueError, KeyError, TypeError) as exc:
                out(self.style.ERROR(f"{line} - unreadable response: {exc}"))
                continue
            named = [e.get("tags", {}).get("name") for e in elements if e.get("tags", {}).get("name")]
            remark = payload.get("remark")
            out(self.style.SUCCESS(f"{line} - results: {len(elements)}"
                                   + (f", first: {named[0]}" if named else "")
                                   + (f" (remark: {remark})" if remark else "")))
            any_ok = any_ok or bool(elements)
        if any_ok:
            out(self.style.SUCCESS("OK: at least one host works, so the page can search."))
        else:
            out(self.style.ERROR("FAILED: no host returned places. The page will say "
                                 "\"Restaurant search is unavailable right now\"."))
