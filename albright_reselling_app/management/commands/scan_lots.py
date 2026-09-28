"""Usage:
    python manage.py scan_lots                          # ShopGoodwill, coin keywords
    python manage.py scan_lots --no-llm                 # regex only, zero API cost
    python manage.py scan_lots --keyword "morgan dollar"  # one keyword, handy for testing
"""
from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from albright_reselling_app.scanner.pipeline import run_scan
from albright_reselling_app.scanner_models import ScanRun


def _stopped_early(keywords_requested, failed_keywords):
    """True if the scan broke off before attempting every requested keyword.
    With the current pipeline, that only happens when two keywords in a row
    fail with SourceUnavailable (a SourceBlocked stop isn't reflected in
    failed_keywords, so it isn't detected here)."""
    keywords_requested = list(keywords_requested)
    if len(failed_keywords) < 2:
        return False
    second_last, last = failed_keywords[-2], failed_keywords[-1]
    try:
        idx = keywords_requested.index(last)
    except ValueError:
        return False
    if idx == 0 or keywords_requested[idx - 1] != second_last:
        return False  # not actually back-to-back in the requested order
    return idx < len(keywords_requested) - 1  # keywords were left unattempted


class Command(BaseCommand):
    help = "Scan auction sources for lots under max bid"

    def add_arguments(self, parser):
        parser.add_argument("--source", default="shopgoodwill")
        parser.add_argument("--category", default="coins")
        parser.add_argument("--no-llm", action="store_true")
        parser.add_argument("--keyword", action="append", help="Override configured keywords (repeatable)")

    def handle(self, *args, **opts):
        run = ScanRun.objects.create(source=opts["source"])
        try:
            result = run_scan(opts["source"], opts["category"], use_llm=not opts["no_llm"],
                               keywords=opts["keyword"])
        except Exception as exc:
            run.error = str(exc)
            run.finished_at = timezone.now()
            run.save()
            raise

        failed_keywords = result.get("failed_keywords") or []
        keywords_requested = opts["keyword"] or settings.RESELLING_SCANNER["KEYWORDS"][opts["category"]]

        run.finished_at = timezone.now()
        run.lots_seen = result["seen"]
        run.candidates = len(result["candidates"])
        run.llm_calls = result["llm_calls"]
        run.failed_keywords = failed_keywords
        run.stopped_early = _stopped_early(keywords_requested, failed_keywords)
        run.save()

        spot = ", ".join(f"{k} ${v:,.2f}" for k, v in result["spot"].items())
        self.stdout.write(f"Spot: {spot}")
        self.stdout.write(f"Lots seen: {result['seen']}   LLM calls: {result['llm_calls']}")
        self.stdout.write(f"Candidates: {len(result['candidates'])}")

        if failed_keywords:
            self.stdout.write(f"Failed keywords: {', '.join(failed_keywords)}")

        if run.stopped_early:
            self.stdout.write(
                "Scan stopped early: 2 consecutive keywords failed (site may be down or throttling us)"
            )

        for ev in result["candidates"]:
            lot = ev.lot
            self.stdout.write(
                f"  ${lot.current_price} now | max ${ev.max_bid} | +${ev.headroom} | {ev.confidence}"
                f" | {lot.title[:70]}\n    {lot.url}"
            )
