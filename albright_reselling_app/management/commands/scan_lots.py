"""Usage:
    python manage.py scan_lots                          # ShopGoodwill, coin keywords
    python manage.py scan_lots --no-llm                 # regex only, zero API cost
    python manage.py scan_lots --keyword "morgan dollar"  # one keyword, handy for testing
"""
from django.core.management.base import BaseCommand

from albright_reselling_app.scanner.pipeline import run_scan


class Command(BaseCommand):
    help = "Scan auction sources for lots under max bid"

    def add_arguments(self, parser):
        parser.add_argument("--source", default="shopgoodwill")
        parser.add_argument("--category", default="coins")
        parser.add_argument("--no-llm", action="store_true")
        parser.add_argument("--keyword", action="append", help="Override configured keywords (repeatable)")

    def handle(self, *args, **opts):
        result = run_scan(opts["source"], opts["category"], use_llm=not opts["no_llm"], keywords=opts["keyword"])
        spot = ", ".join(f"{k} ${v:,.2f}" for k, v in result["spot"].items())
        self.stdout.write(f"Spot: {spot}")
        self.stdout.write(f"Lots seen: {result['seen']}   LLM calls: {result['llm_calls']}")
        self.stdout.write(f"Candidates: {len(result['candidates'])}")
        for ev in result["candidates"]:
            lot = ev.lot
            self.stdout.write(
                f"  ${lot.current_price} now | max ${ev.max_bid} | +${ev.headroom} | {ev.confidence}"
                f" | {lot.title[:70]}\n    {lot.url}"
            )
