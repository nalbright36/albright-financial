"""Recount each person's cached storage use (UserStorage.used_bytes) from their attachments - file plus thumbnail -
and print anyone whose cached value was wrong (now fixed).

    python manage.py recalc_storage_usage
    python manage.py recalc_storage_usage --user nate
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from albright_labs.storage import format_bytes, recalculate


class Command(BaseCommand):
    help = "Recompute every user's (or one user's) storage use from their attachments; prints the ones that were wrong."

    def add_arguments(self, parser):
        parser.add_argument("--user", metavar="USERNAME", help="Only this user.")

    def handle(self, *args, user=None, **options):
        users = None
        if user:
            found = get_user_model().objects.filter(username=user).first()
            if found is None:
                raise CommandError(f"No user named {user!r}.")
            users = [found]
        wrong = recalculate(users)
        for person, cached, actual in wrong:
            self.stdout.write(f"{person.get_username()}: was {format_bytes(cached)} ({cached} B), "
                              f"now {format_bytes(actual)} ({actual} B)")
        self.stdout.write(self.style.SUCCESS(
            "Every cached value was right." if not wrong else
            f"Fixed {len(wrong)} user{'s' if len(wrong) != 1 else ''}."))
