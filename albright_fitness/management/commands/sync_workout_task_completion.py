"""Fitness workout tasks already checked off on the Planner with no finished workout for them get a "Completed from
Planner" one (workout_sync.backfill) - so they count in Fitness (Today, This week, the streak). Safe to run again:
an occurrence with a workout is left alone. Migration 0018 runs it once."""
from datetime import date

from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from albright_fitness import workout_sync


class Command(BaseCommand):
    help = "Make a 'Completed from Planner' workout for each checked-off Fitness workout task that has none."

    def add_arguments(self, parser):
        parser.add_argument("--since", help="Only occurrences on or after this date (YYYY-MM-DD).")
        parser.add_argument("--dry-run", action="store_true", help="Count them; change nothing.")

    def handle(self, *args, **options):
        since = None
        if options.get("since"):
            try:
                since = date.fromisoformat(options["since"])
            except ValueError:
                raise CommandError("--since must be YYYY-MM-DD.")
        made = workout_sync.backfill(apps.get_model, since=since, dry_run=options["dry_run"])
        verb = "Would make" if options["dry_run"] else "Made"
        self.stdout.write(f"{verb} {made} 'Completed from Planner' workout{'' if made == 1 else 's'}.")
