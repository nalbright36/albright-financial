"""Usage:
    python manage.py prefill_profile

One-time setup for the Inbox/Pipeline workflow (safe to run again):
fills My profile's empty fields from my resume (my_profile.RESUME_PREFILL -
nothing I've typed is overwritten), adds the default job sources (each
inherits its site's "terms checked"), and recomputes work types, Hidden
reasons, duplicates and expiry for what's already in the Inbox. No AI calls.
"""
from django.core.management.base import BaseCommand

from albright_crypto_app import relevance
from albright_crypto_app.models import MyProfile, Source
from albright_crypto_app.my_profile import prefill_profile
from albright_crypto_app.pipeline import sync_default_income_sources, sync_sources


class Command(BaseCommand):
    help = "Prefill My profile from the resume, add the default job sources, and tidy the Inbox."

    def handle(self, *args, **opts):
        changed = prefill_profile(MyProfile.load())
        self.stdout.write(f"Profile: {', '.join(changed) if changed else 'nothing empty to fill'}")
        before = Source.objects.count()
        sync_sources()
        sync_default_income_sources()
        self.stdout.write(f"Sources added: {Source.objects.count() - before}")
        result = relevance.refresh_inbox()
        self.stdout.write(f"Inbox: {result['updated']} updated, {result['merged']} duplicates merged, "
                          f"{result['expired']} expired")
