"""Data migration: the only automatic data change in this feature. Every
existing LedgerEntry gets a `status` of "sold_out" if it already has a
sold_for value, or "holding" otherwise (the field's own default, applied
by the previous migration, is already "holding" for every row - this just
promotes the ones that were already sold)."""
from django.db import migrations


def backfill_status(apps, schema_editor):
    LedgerEntry = apps.get_model("albright_reselling_app", "LedgerEntry")
    LedgerEntry.objects.filter(sold_for__isnull=False).update(status="sold_out")


def noop_reverse(apps, schema_editor):
    """Nothing to reverse - unmigrating removes the `status` column itself
    (via 0018's reverse), so there's no prior state to restore here."""


class Migration(migrations.Migration):

    dependencies = [
        ('albright_reselling_app', '0018_add_ledger_scanner_integration'),
    ]

    operations = [
        migrations.RunPython(backfill_status, noop_reverse),
    ]
