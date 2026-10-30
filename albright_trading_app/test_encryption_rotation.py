"""Secrets live only in .env: required settings fail loudly when missing;
the credentials cipher is a MultiFernet (encrypt with the current key, read
old ones during a rotation); and rotate_encryption_key re-encrypts every
stored Alpaca credential and the Gmail token with the new key - atomically,
with a dry run, never printing plaintext."""
import io
import os
from unittest import mock

from cryptography.fernet import Fernet
from django.contrib.auth.models import User
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from albright_crypto_app.models import GmailConnection
from albright_trading_app.crypto_utils import get_fernet, primary_fernet
from albright_trading_app.models import AlpacaCredentials
from albright_trading_proj import settings as project_settings
from albright_labs.testing import with_apps

OLD, NEW, OTHER = (Fernet.generate_key().decode() for _ in range(3))
SECRET = "sk-not-real-alpaca-secret-123"


def keys(primary, *old):
    return override_settings(ALPACA_CREDENTIALS_ENCRYPTION_KEY=primary, ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS=list(old))


class RequiredSettingsTests(TestCase):
    def test_missing_required_env_fails_with_a_clear_error(self):
        with mock.patch.dict(os.environ, {"SECRET_KEY": ""}):
            with self.assertRaisesMessage(ImproperlyConfigured, "SECRET_KEY is not set"):
                project_settings.required_env("SECRET_KEY")
        with mock.patch.dict(os.environ, {"SOME_NEW_THING": "  value  "}):
            self.assertEqual(project_settings.required_env("SOME_NEW_THING"), "value")

    def test_settings_file_has_no_secrets_left(self):
        source = open(project_settings.__file__, encoding="utf-8").read()
        for marker in ("django-insecure", "RECAPTCHA", "ondigitalocean", "doadmin"):
            self.assertNotIn(marker, source)
        # The email password may only ever be read from .env, never written here.
        password_lines = [line.strip() for line in source.splitlines() if "EMAIL_HOST_PASSWORD" in line]
        self.assertEqual(password_lines,
                         ['EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "").strip()'])
        for name in ("SECRET_KEY", "ALPACA_CREDENTIALS_ENCRYPTION_KEY", "DB_HOST", "DB_PASSWORD"):
            self.assertIn(f'required_env("{name}")', source)

    def test_bad_fernet_key_is_rejected_at_startup(self):
        with mock.patch.object(project_settings, "ALPACA_CREDENTIALS_ENCRYPTION_KEY", "not-a-key"):
            with self.assertRaisesMessage(ImproperlyConfigured, "isn't a valid Fernet key"):
                project_settings._check_fernet_keys()

    def test_env_example_lists_every_required_name_without_values(self):
        path = os.path.join(os.path.dirname(os.path.dirname(project_settings.__file__)), ".env.example")
        lines = [l for l in open(path, encoding="utf-8").read().splitlines() if l and not l.startswith("#")]
        names = {l.split("=", 1)[0] for l in lines}
        for name in ("SECRET_KEY", "ALPACA_CREDENTIALS_ENCRYPTION_KEY", "ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS",
                     "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT"):
            self.assertIn(name, names)
        self.assertTrue(all(l.split("=", 1)[1] == "" for l in lines), "example values must be empty")


class MultiFernetTests(TestCase):
    def test_encrypts_with_new_and_reads_old_during_the_switch(self):
        old_token = Fernet(OLD.encode()).encrypt(b"x")
        with keys(NEW, OLD):
            self.assertEqual(get_fernet().decrypt(old_token), b"x")
            fresh = get_fernet().encrypt(b"y")
            self.assertEqual(primary_fernet().decrypt(fresh), b"y")   # new values use the new key
        with keys(NEW):
            with self.assertRaises(Exception):
                get_fernet().decrypt(old_token)                       # old key removed: old values unreadable

    def test_missing_key_is_a_clear_error(self):
        with keys(""):
            with self.assertRaisesMessage(ImproperlyConfigured, "ALPACA_CREDENTIALS_ENCRYPTION_KEY is not set"):
                get_fernet()


class RotateEncryptionKeyTests(TestCase):
    def setUp(self):
        with keys(OLD):
            self.creds = AlpacaCredentials(user=with_apps(User.objects.create_user(username="trader", password="pw-not-real-1")),
                                           is_paper=True)
            self.creds.set_credentials("PKNOTREAL123", SECRET)
            self.creds.save()
            self.gmail = GmailConnection(pk=GmailConnection.SINGLETON_PK, email="me@example.com", scope="readonly")
            self.gmail.set_token({"access_token": "ya29.not-real", "refresh_token": "1//not-real"})
            self.gmail.save()
        self.before = self.raw()

    def raw(self):
        c = AlpacaCredentials.objects.get(pk=self.creds.pk)
        return (c.encrypted_api_key, c.encrypted_secret_key, GmailConnection.objects.get().encrypted_token)

    def run_command(self, *args):
        out = io.StringIO()
        call_command("rotate_encryption_key", *args, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_and_writes_nothing(self):
        with keys(NEW, OLD):
            output = self.run_command("--dry-run")
        self.assertIn("3 value(s) would be re-encrypted", output)
        self.assertIn("Dry run - nothing was written.", output)
        self.assertEqual(self.raw(), self.before)

    def test_rotation_re_encrypts_everything_with_the_new_key(self):
        updated_at = GmailConnection.objects.get().updated_at
        with keys(NEW, OLD):
            output = self.run_command()
        self.assertIn("3 value(s) re-encrypted", output)
        self.assertNotIn(SECRET, output)                              # plaintext never printed
        self.assertNotIn("ya29", output)
        after = self.raw()
        self.assertTrue(all(a != b for a, b in zip(after, self.before)))
        with keys(NEW):                                               # old key gone: everything still readable
            creds = AlpacaCredentials.objects.get(pk=self.creds.pk)
            self.assertEqual((creds.get_api_key(), creds.get_secret_key()), ("PKNOTREAL123", SECRET))
            self.assertEqual(GmailConnection.objects.get().get_token()["refresh_token"], "1//not-real")
        self.assertEqual(GmailConnection.objects.get().updated_at, updated_at)   # only the ciphertext changed
        with keys(NEW, OLD):
            again = self.run_command()                                # safe to run twice
        self.assertIn("0 value(s) re-encrypted with the current key; 3 already on it", again)

    def test_an_unreadable_value_aborts_the_whole_run(self):
        AlpacaCredentials.objects.filter(pk=self.creds.pk).update(
            encrypted_secret_key=Fernet(OTHER.encode()).encrypt(b"lost").decode())
        before = self.raw()
        with keys(NEW, OLD):
            with self.assertRaisesMessage(CommandError, "nothing was changed"):
                self.run_command()
        self.assertEqual(self.raw(), before)                          # the readable ones weren't touched either

    def test_without_the_old_key_nothing_can_be_rotated(self):
        with keys(NEW):
            with self.assertRaises(CommandError):
                self.run_command("--dry-run")
        self.assertEqual(self.raw(), self.before)
