"""Usage:
    python manage.py export_resume_data              # every albright_resume_* table -> ~/retired_app_exports/
    python manage.py export_resume_data --out DIR    # somewhere else (never under static/ or media/)

The public resume was removed. Before its tables are dropped (migration albright_resume 0004), this saves every row of
every albright_resume_* table - the resume content, the AI calls and drafts, the question log, the chat conversations
and messages - as one JSON file per table, in a new timestamped folder:

    ~/retired_app_exports/albright_resume-<YYYYmmdd-HHMMSS>/<table>.json  (+ manifest.json with the row counts)

It reads with plain SQL (the models are gone), changes nothing, and never overwrites an earlier export. After 0004 has
run there's nothing left to export, and it says so.
"""
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone

PREFIX = "albright_resume_"


def _json(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    if isinstance(value, (bytes, memoryview)):
        return bytes(value).hex()
    raise TypeError(f"Can't export a {type(value).__name__}")


def default_out_dir():
    return Path.home() / "retired_app_exports"


class Command(BaseCommand):
    help = "Save every row of the retired public resume's tables to ~/retired_app_exports/ (before migration 0004)."

    def add_arguments(self, parser):
        parser.add_argument("--out", help="Folder to export into (default: ~/retired_app_exports).")

    def handle(self, *args, out=None, **opts):
        base = Path(out).expanduser().resolve() if out else default_out_dir()
        for public in (settings.STATIC_ROOT, settings.MEDIA_ROOT, *getattr(settings, "STATICFILES_DIRS", [])):
            if public and (base == Path(public).resolve() or Path(public).resolve() in base.parents):
                raise CommandError(f"{base} is inside a public folder ({public}) - export somewhere private.")
        tables = sorted(t for t in connection.introspection.table_names() if t.startswith(PREFIX))
        if not tables:
            self.stdout.write("No albright_resume tables left - nothing to export (migration 0004 already ran).")
            return
        folder = base / f"albright_resume-{timezone.now():%Y%m%d-%H%M%S}"
        folder.mkdir(parents=True, exist_ok=False)
        counts = {}
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(f"SELECT * FROM {connection.ops.quote_name(table)}")
                columns = [col[0] for col in cursor.description]
                rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
                (folder / f"{table}.json").write_text(json.dumps(rows, default=_json, indent=1), encoding="utf-8")
                counts[table] = len(rows)
        manifest = {"app": "albright_resume", "exported_at": timezone.now().isoformat(), "rows": counts}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        for table, n in counts.items():
            self.stdout.write(f"  {table}: {n}")
        self.stdout.write(self.style.SUCCESS(f"Exported {sum(counts.values())} row(s) from {len(counts)} table(s) "
                                             f"to {folder}"))
