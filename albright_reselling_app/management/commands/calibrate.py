"""Usage:
    python manage.py calibrate

Compares real outcomes (Ledger sale prices, closed-lot final prices) to
what settings.py's RESALE_MULTIPLIERS/CATEGORY_FEES predicted, and saves a
CalibrationSuggestion wherever the gap is both big enough to matter (more
than 5%) and backed by enough samples - see scanner/insights.py for the
actual comparisons. Never writes directly to settings.py or to
CalibrationOverride; suggestions sit pending until applied (or dismissed)
from the Insights page. Safe to run repeatedly (e.g. nightly) - a key that
already has a pending suggestion is left alone, not duplicated.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from albright_reselling_app.scanner.insights import build_calibration_suggestions


class Command(BaseCommand):
    help = "Compare real outcomes to settings.py's calibration values and suggest adjustments."

    def handle(self, *args, **opts):
        created = build_calibration_suggestions(settings.RESELLING_SCANNER)

        if not created:
            self.stdout.write("No new suggestions (nothing moved enough, or already pending).")
            return

        self.stdout.write(f"{len(created)} new suggestion(s):")
        for suggestion in created:
            self.stdout.write(
                f"  {suggestion.key}: {suggestion.current_value} -> {suggestion.suggested_value} "
                f"(n={suggestion.sample_size})"
            )
