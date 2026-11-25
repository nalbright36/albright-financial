"""Opt active circle members into Meals and Money when the circle has the app enabled and they
can use it. That is the missing step when someone accepted an invite without ticking Recipes
(the boxes used to start unchecked) or when the owner turned the app on later.

Idempotent and safe to re-run. Prints counts only — no names, emails, or recipe titles.

  python manage.py opt_in_circle_space_apps --dry-run
  python manage.py opt_in_circle_space_apps
"""
from django.core.management.base import BaseCommand

from albright_labs import circles


class Command(BaseCommand):
    help = "Opt active members into a circle's Meals/Money space when they can use the app (counts only)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Print how many memberships would be updated; change nothing.")

    def handle(self, *args, **options):
        dry = bool(options.get("dry_run"))
        counts = circles.opt_in_members_for_apps(dry_run=dry)
        if not counts:
            self.stdout.write("No space-backed apps to check.")
            return
        parts = []
        total = 0
        for app, row in counts.items():
            total += row["memberships"]
            parts.append(f"{row['memberships']} membership(s) in {row['circles']} circle(s) for {app}")
        summary = "; ".join(parts) + "."
        if dry:
            self.stdout.write("Dry run — would opt in " + summary + " No changes made.")
        elif total:
            self.stdout.write("Opted in " + summary)
        else:
            self.stdout.write("Nothing to update — every eligible member is already opted in.")
