"""Print a new VAPID key pair for Web Push. Does not write any file or change settings.

    python manage.py generate_vapid_keys

Copy the three lines into .env (local) or the PythonAnywhere Web tab, then reload.
Never commit the keys.
"""
from cryptography.hazmat.primitives import serialization
from django.core.management.base import BaseCommand
from py_vapid import Vapid
from py_vapid.utils import b64urlencode


class Command(BaseCommand):
    help = "Print a new VAPID key pair for Web Push (to copy into env vars). Does not save the keys."

    def handle(self, *args, **opts):
        vapid = Vapid()
        vapid.generate_keys()
        private = b64urlencode(vapid.private_key.private_numbers().private_value.to_bytes(32, "big"))
        public = b64urlencode(vapid.public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        ))
        self.stdout.write("Add these to your .env (local) or the PythonAnywhere Web tab, then reload the app.")
        self.stdout.write("Never commit these keys. This command does not save them.\n")
        self.stdout.write(f"VAPID_PUBLIC_KEY={public}")
        self.stdout.write(f"VAPID_PRIVATE_KEY={private}")
        self.stdout.write("VAPID_SUBJECT=mailto:you@example.com")
        self.stdout.write("\nUse a contact email you control in VAPID_SUBJECT (mailto:...).")
