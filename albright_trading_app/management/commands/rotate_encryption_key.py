"""Usage:
    python manage.py rotate_encryption_key --dry-run   # check: what would change, nothing written
    python manage.py rotate_encryption_key             # re-encrypt everything with the current key

Re-encrypts every value stored with the credentials encryption key:

  - AlpacaCredentials.encrypted_api_key / encrypted_secret_key (albright_trading_app)
  - GmailConnection.encrypted_token (albright_crypto_app)

Set up .env first: ALPACA_CREDENTIALS_ENCRYPTION_KEY = the NEW key, and
ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS = the OLD key (comma-separate several).
Each value is decrypted with whichever key opens it (old or new) and
re-encrypted with the new key, all inside one transaction: if any value can't
be decrypted with the configured keys, or a re-encrypted value doesn't decrypt
back to exactly the same plaintext with the new key alone, NOTHING is written.
Values already on the new key are left alone, so running it twice is safe.
Plaintext is never printed or logged - only counts.

After a real run reports "0 still on an old key", remove
ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS from .env everywhere and restart.
"""
from cryptography.fernet import InvalidToken
from django.apps import apps
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from albright_trading_app.crypto_utils import get_fernet, old_fernets, primary_fernet

# Every encrypted field: (app_label.Model, [fields]). Add new ones here.
ENCRYPTED_FIELDS = [
    ("albright_trading_app.AlpacaCredentials", ["encrypted_api_key", "encrypted_secret_key"]),
    ("albright_crypto_app.GmailConnection", ["encrypted_token"]),
]


class DryRunRollback(Exception):
    """Raised inside the transaction so a dry run writes nothing."""


def _opens(fernet, token):
    try:
        return fernet.decrypt(token)
    except InvalidToken:
        return None


class Command(BaseCommand):
    help = "Re-encrypt stored credentials (Alpaca keys, Gmail token) with the current encryption key."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Check every value and report what would change; write nothing.")

    def handle(self, *args, dry_run=False, **opts):
        primary, multi = primary_fernet(), get_fernet()
        if not old_fernets():
            self.stdout.write(self.style.WARNING(
                "ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS is empty - only values already on the current key can be "
                "read. Set it to the old key to rotate."))
        counts = {"rotated": 0, "already_current": 0, "empty": 0}
        unreadable = []
        try:
            with transaction.atomic():
                for label, fields in ENCRYPTED_FIELDS:
                    model = apps.get_model(label)
                    for row in model.objects.select_for_update().order_by("pk"):
                        updates = {}
                        for field in fields:
                            token = (getattr(row, field) or "").encode()
                            if not token:
                                counts["empty"] += 1
                                continue
                            if _opens(primary, token) is not None:
                                counts["already_current"] += 1
                                continue
                            plaintext = _opens(multi, token)
                            if plaintext is None:
                                unreadable.append(f"{label} #{row.pk} {field}")
                                continue
                            rotated = multi.rotate(token)
                            if _opens(primary, rotated) != plaintext:   # belt and braces: round-trips exactly
                                raise CommandError(f"Re-encryption check failed for {label} #{row.pk} {field} - "
                                                   "nothing was changed.")
                            updates[field] = rotated.decode()
                            counts["rotated"] += 1
                        if updates:
                            # update(), not save(): no auto_now/side effects, just the ciphertext.
                            model.objects.filter(pk=row.pk).update(**updates)
                if unreadable:
                    raise CommandError(
                        "These can't be decrypted with the current key or any key in "
                        "ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS - nothing was changed:\n  " + "\n  ".join(unreadable))
                if dry_run:
                    raise DryRunRollback
        except DryRunRollback:
            pass
        verb = "would be re-encrypted" if dry_run else "re-encrypted"
        self.stdout.write(f"{counts['rotated']} value(s) {verb} with the current key; "
                          f"{counts['already_current']} already on it; {counts['empty']} empty.")
        still_old = counts["rotated"] if dry_run else 0
        self.stdout.write(f"{still_old} still on an old key." + (
            " Dry run - nothing was written." if dry_run else
            " Remove ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS from .env (every machine) and restart."))
