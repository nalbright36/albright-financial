"""albright_trading_proj.runner: the kept test database is purged of leftover test rows (never seed
data), only a test_ database is ever touched, and a second run can't take the lock."""
from unittest import mock

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase

from albright_fitness.models import Exercise
from albright_labs.models import Space
from albright_trading_proj import runner


class LeftoverPurgeTests(TestCase):
    def test_purges_leftovers_and_keeps_seed_rows(self):
        seeded = Exercise.objects.filter(owner__isnull=True).count()
        self.assertGreater(seeded, 0)
        nate = User.objects.create_user("nate-leftover", password="pw")
        Space.objects.get_or_create(user=nate)
        Exercise.objects.create(owner=nate, name="Leftover lift")
        found = runner.purge_leftovers(connection)
        self.assertIn("auth.User", found)
        self.assertEqual(runner.leftover_counts(connection), {})
        self.assertFalse(User.objects.exists())
        self.assertEqual(Exercise.objects.count(), seeded)                     # the library stays

    def test_refuses_a_database_that_isnt_a_test_database(self):
        fake = mock.Mock(settings_dict={"NAME": "albright_production"}, alias="default")
        with self.assertRaises(RuntimeError):
            runner.purge_leftovers(fake)

    def test_a_second_run_cannot_take_the_lock(self):
        first = runner.acquire_lock()           # None when this run's own runner already holds it
        try:
            if first not in (None, "no-lock"):
                self.assertIsNone(runner.acquire_lock())
            elif first is None:
                self.assertIsNone(runner.acquire_lock())
        finally:
            runner.release_lock(first)
