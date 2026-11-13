"""The test runner (settings.TEST_RUNNER). Keeps the kept test database (--keepdb) clean between runs.

Why: the migration tests (albright_labs test_circles / test_sharing) are TransactionTestCases with
serialized_rollback. Django snapshots the WHOLE test database at the start of every run and puts that
snapshot back after each of them. A run killed mid-test, or two runs at once, leaves test rows behind
(users nate/sam/kim, a circle, accounts...); the next run snapshots them, and from then on every run
restores them - so tests that create "nate" fail with duplicate keys, run after run.

So, before any test runs:
  1. take a Postgres advisory lock - a second `manage.py test` against the same test database stops
     with a message instead of flushing tables under the first one;
  2. delete every row that isn't migration seed data (SEED_MODELS) from the test database;
  3. snapshot again, from the clean state.

Only ever touches a database whose name starts with "test_".
"""
import zlib

from django.apps import apps
from django.db import DEFAULT_DB_ALIAS, IntegrityError, connections, transaction
from django.db.models import ProtectedError
from django.test.runner import DiscoverRunner

# Models whose rows come from migrations (and post_migrate), not tests. Everything else in a fresh
# test database is empty. A new data migration that seeds a table needs its model added here.
SEED_MODELS = {
    "auth.Permission",
    "contenttypes.ContentType",
    "albright_resume.ResumeSettings",        # albright_resume 0002_placeholder_content
    "albright_resume.WorkExperience",
    "albright_resume.EducationItem",
    "albright_resume.Project",
    "albright_fitness.Exercise",             # albright_fitness 0002_seed_exercises (the library; owner is null)
}
LOCK_KEY = zlib.crc32(b"albright-test-database")      # any fixed bigint shared by every run


class TestDatabaseInUse(RuntimeError):
    pass


def _check_test_db(connection):
    name = connection.settings_dict.get("NAME") or ""
    if not str(name).startswith("test_"):
        raise RuntimeError(f"Refusing to purge a database that isn't a test database ({connection.alias}).")


def leftover_counts(connection):
    """{model label: rows} for every non-seed model with rows."""
    _check_test_db(connection)
    found = {}
    for model in apps.get_models(include_auto_created=True):
        label = model._meta.label
        if label in SEED_MODELS or not model._meta.managed or model._meta.proxy:
            continue
        count = model._base_manager.using(connection.alias).count()
        if count:
            found[label] = count
    return found


def purge_leftovers(connection, passes=5):
    """Delete every non-seed row (users first - their cascades take most of the rest), plus user-owned
    seed-table rows. Returns {model label: rows found}. Raises if anything is left after `passes`."""
    _check_test_db(connection)
    found = leftover_counts(connection)
    if not found:
        return {}
    alias = connection.alias
    for _ in range(passes):
        remaining = leftover_counts(connection)
        if not remaining:
            break
        ordered = sorted(remaining, key=lambda label: label != "auth.User")
        for label in ordered:
            model = apps.get_model(label)
            try:
                with transaction.atomic(using=alias):
                    model._base_manager.using(alias).all().delete()
            except (ProtectedError, IntegrityError):
                continue                        # something still points at it; the next pass gets it
    remaining = leftover_counts(connection)
    if remaining:
        raise RuntimeError(f"Couldn't clear leftover test rows: {remaining}. Drop the test database and rerun.")
    return found


def acquire_lock(alias=DEFAULT_DB_ALIAS):
    """A dedicated connection holding the run's advisory lock, or None if another run holds it.
    Postgres only (returns a no-op marker elsewhere). Closing the connection - or the process
    dying - releases it."""
    base = connections[alias]
    if base.vendor != "postgresql":
        return "no-lock"
    holder = base.copy(alias="albright_test_lock")
    holder.ensure_connection()
    with holder.cursor() as cursor:
        cursor.execute("SELECT pg_try_advisory_lock(%s)", [LOCK_KEY])
        got = cursor.fetchone()[0]
    if not got:
        holder.close()
        return None
    return holder


def release_lock(holder):
    if holder in (None, "no-lock"):
        return
    try:
        with holder.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s)", [LOCK_KEY])
    finally:
        holder.close()


class AlbrightTestRunner(DiscoverRunner):
    def setup_databases(self, **kwargs):
        self._lock = acquire_lock()
        if self._lock is None:
            raise TestDatabaseInUse("Another test run is using the test database. Wait for it to finish "
                                    "(or stop it) and run the tests again.")
        try:
            config = super().setup_databases(**kwargs)
            for alias in connections:
                connection = connections[alias]
                if connection.settings_dict.get("TEST", {}).get("MIRROR"):
                    continue
                found = purge_leftovers(connection)
                if found:
                    if self.verbosity >= 1:
                        rows = ", ".join(f"{label} {n}" for label, n in sorted(found.items()))
                        print(f"Removed leftover rows from the test database ({alias}): {rows}")
                    if hasattr(connection, "_test_serialized_contents"):
                        connection._test_serialized_contents = connection.creation.serialize_db_to_string()
            return config
        except BaseException:
            release_lock(self._lock)
            raise

    def teardown_databases(self, old_config, **kwargs):
        try:
            super().teardown_databases(old_config, **kwargs)
        finally:
            release_lock(getattr(self, "_lock", None))
