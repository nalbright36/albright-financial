"""Archived apps (albright_labs.archive, docs/ARCHIVED_APPS.md) and the removed public resume.

The test runner clears ARCHIVED_APPS so every app's own tests run; these switch archiving on with override_settings."""
import io
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management import call_command, get_commands, load_command_class
from django.db import connection
from django.test import Client, TestCase, override_settings
from django.urls import reverse

from albright_bot_api.models import BotToken
from albright_labs import app_access, archive, launcher
from albright_labs.models import UserPreference
from albright_labs.testing import home_tiles, launcher_tiles, with_apps

ARCHIVED = override_settings(ARCHIVED_APPS=["trading", "reselling"])
PW = "pw-not-real-12345"
BANNER = "Archived app - hidden from everyone else"


def _commands(package):
    return sorted(name for name, app in get_commands().items() if app == package)


def _load(package, name):
    """The command class, or None when it needs a package this machine doesn't have (run_scan_worker: playwright)."""
    try:
        return load_command_class(package, name)
    except ModuleNotFoundError:
        return None


class SettingTests(TestCase):
    def test_trading_and_reselling_are_archived_by_default(self):
        if "ARCHIVED_APPS" in os.environ:
            self.skipTest("ARCHIVED_APPS is set in this environment")
        self.assertEqual(settings.ARCHIVED_APPS_DEPLOYED, ["trading", "reselling"])

    @ARCHIVED
    def test_is_app_archived_takes_any_of_an_apps_names(self):
        for name in ("trading", "trade", "Trading", "albright_trading_app", "reselling", "resell", "reseller"):
            self.assertTrue(archive.is_app_archived(name), name)
        for name in ("meals", "planner", "opps", "admin", "nonsense", "", None):
            self.assertFalse(archive.is_app_archived(name), name)
        self.assertEqual(archive.archived_keys(), {"trade", "resell"})

    @override_settings(ARCHIVED_APPS=[])
    def test_nothing_is_archived_when_the_setting_is_empty(self):
        self.assertFalse(archive.is_app_archived("trading"))
        self.assertEqual(archive.archived_keys(), frozenset())


class HiddenTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "", PW)
        self.pat = with_apps(User.objects.create_user("pat", password=PW))     # every app, Trading and Reselling too

    @ARCHIVED
    def test_not_in_home_launcher_or_nav_for_anyone(self):
        for user in (self.boss, self.pat):
            with self.subTest(user=user.username):
                client = Client()
                client.force_login(user)
                content = client.get(reverse("labs:hub")).content.decode()
                self.assertFalse({"trade", "resell"} & set(home_tiles(content, include_hidden=True)))
                targets = [target for target, _label in launcher_tiles(content)]
                self.assertNotIn("trading", targets)
                self.assertNotIn("reselling", targets)
                self.assertIn("meals", targets)
                for text in (">Trading<", ">Reselling<", ">Resume<", reverse("trading_home"),
                             reverse("albright_reselling_app:dashboard")):
                    self.assertNotIn(text, content)
                pref = UserPreference.for_user(user)
                self.assertFalse({"trade", "resell"} & set(app_access.nav_keys(user, pref)))
                self.assertFalse({"trade", "resell"} & set(app_access.allowed_keys(user, pref)))

    @ARCHIVED
    def test_people_keep_their_stored_entries(self):
        pref = UserPreference.for_user(self.pat)
        self.assertIn("trade", pref.allowed_apps)
        # Admin > Users saving their access (the form has no archived apps) keeps the archived entries.
        app_access.grant(pref, ["meals", "notes"])
        self.assertEqual(set(pref.allowed_apps), {"meals", "notes", "trade", "resell"})

    @ARCHIVED
    def test_admin_users_page_has_no_archived_columns(self):
        client = Client()
        client.force_login(self.boss)
        content = client.get(reverse("albright_control:users")).content.decode()
        self.assertNotIn("<th>Trading</th>", content)
        self.assertNotIn("<th>Reselling</th>", content)
        self.assertIn("<th>Meals</th>", content)

    def test_restoring_brings_it_back(self):
        pref = UserPreference.for_user(self.pat)
        with override_settings(ARCHIVED_APPS=["reselling"]):
            self.assertIn("trade", app_access.nav_keys(self.pat, pref))
            self.assertNotIn("resell", app_access.nav_keys(self.pat, pref))
            client = Client()
            client.force_login(self.pat)
            targets = [t for t, _ in launcher_tiles(client.get(reverse("labs:hub")).content.decode())]
            self.assertIn("trading", targets)
            self.assertNotIn("reselling", targets)
        with override_settings(ARCHIVED_APPS=[]):
            keys = [item["key"] for item in launcher.launcher_items(self.pat, pref, "labs")]
            self.assertIn("trade", keys)
            self.assertIn("resell", keys)

    @ARCHIVED
    def test_default_app_falls_back_to_home(self):
        for landing, page in (("app", "trade"), ("page", "resell:dashboard")):
            with self.subTest(landing=landing):
                UserPreference.objects.filter(user=self.pat).update(landing=landing, landing_page=page)
                client = Client()
                client.force_login(self.pat)
                response = client.get(reverse("labs:root"))
                self.assertEqual(response.status_code, 200)              # the Home picker, not a redirect
                self.assertIn('id="ht-grid"', response.content.decode())
        UserPreference.objects.filter(user=self.pat).update(landing="last_app", landing_page="")
        client = Client()
        client.force_login(self.pat)
        client.cookies["last_app"] = "trading"
        self.assertEqual(client.get(reverse("labs:root")).status_code, 200)


class PageTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "", PW)
        self.pat = with_apps(User.objects.create_user("pat", password=PW))

    def urls(self):
        return [reverse("trading_home"), reverse("strategies"), reverse("grok_bot"),
                reverse("albright_reselling_app:dashboard"), reverse("albright_reselling_app:calculator")]

    @ARCHIVED
    def test_404_archived_page_for_people_and_visitors(self):
        signed_in = Client()
        signed_in.force_login(self.pat)
        for client in (signed_in, Client()):
            for url in self.urls():
                with self.subTest(url=url, signed_in=client is signed_in):
                    response = client.get(url)
                    self.assertEqual(response.status_code, 404)
                    self.assertContains(response, "This app is archived", status_code=404)
        # POSTs too - nothing runs.
        self.assertEqual(signed_in.post(reverse("strategy_create"), {}).status_code, 404)

    @ARCHIVED
    def test_superusers_still_open_it_with_a_banner(self):
        client = Client()
        client.force_login(self.boss)
        response = client.get(reverse("albright_reselling_app:calculator"))
        self.assertContains(response, BANNER)
        # A page that would use the saved Alpaca keys says why instead of using them.
        from albright_trading_app.models import AlpacaCredentials
        creds = AlpacaCredentials(user=self.boss, is_paper=True)
        creds.set_credentials("key-not-real", "secret-not-real")
        creds.save()
        with mock.patch("albright_trading_app.models.get_fernet") as fernet:
            response = client.get(reverse("trading_account"))
            fernet.assert_not_called()                                   # never decrypted
        self.assertContains(response, "saved credentials aren&#x27;t used - this page needs them")

    def test_no_banner_when_not_archived(self):
        client = Client()
        client.force_login(self.boss)
        self.assertNotContains(client.get(reverse("albright_reselling_app:calculator")), BANNER)

    @ARCHIVED
    def test_shared_pages_without_a_namespace_still_work(self):
        client = Client()
        client.force_login(self.pat)
        for name in ("service_worker", "offline", "site_manifest"):
            with self.subTest(name=name):
                self.assertEqual(client.get(reverse(name)).status_code, 200)
        self.assertEqual(Client().get(reverse("user_login")).status_code, 200)
        self.assertEqual(Client().get(reverse("password_reset")).status_code, 200)

    @ARCHIVED
    def test_stored_alpaca_keys_are_never_decrypted(self):
        from albright_trading_app.models import AlpacaCredentials
        creds = AlpacaCredentials(user=self.boss, is_paper=True)
        creds.set_credentials("key-not-real", "secret-not-real")
        with self.assertRaises(archive.ArchivedAppError):
            creds.get_api_key()
        with self.assertRaises(archive.ArchivedAppError):
            creds.get_secret_key()


class CommandTests(TestCase):
    def test_every_archived_app_command_is_guarded(self):
        for package, slug in (("albright_trading_app", "trading"), ("albright_reselling_app", "reselling")):
            for name in _commands(package):
                if name == "rotate_encryption_key":       # site-wide: also Gmail and AI assistant keys
                    continue
                with self.subTest(command=name):
                    command = _load(package, name)
                    if command is None:                 # can't import it here: check its source has the guard
                        source = Path(settings.BASE_DIR) / package / "management" / "commands" / f"{name}.py"
                        self.assertIn(f'@skip_if_archived("{slug}")', source.read_text(encoding="utf-8"))
                        continue
                    self.assertEqual(getattr(command, "archived_app_slug", None), slug)

    @ARCHIVED
    def test_archived_commands_exit_without_doing_anything(self):
        from albright_control.models import JobRun
        for package in ("albright_trading_app", "albright_reselling_app"):
            for name in _commands(package):
                if name == "rotate_encryption_key" or _load(package, name) is None:
                    continue
                with self.subTest(command=name), mock.patch("requests.post") as post, \
                        mock.patch("requests.get") as get:
                    out = io.StringIO()
                    args = ["no-such-workbook.xlsx"] if name == "import_personal_ledger" else []   # a required path
                    call_command(name, *args, stdout=out)
                    self.assertEqual(out.getvalue().strip(), archive.SKIP_MESSAGE)
                    post.assert_not_called()
                    get.assert_not_called()
        self.assertFalse(JobRun.objects.exists())

    @ARCHIVED
    def test_admin_health_and_integrations_leave_them_out(self):
        from albright_control import integrations, jobs
        names = [row["job"] for row in jobs.health_rows()]
        self.assertFalse([n for n in names if n.startswith("scan_lots") or n in dict(jobs.RESELLING_JOBS)])
        self.assertIn("prune_activity", names)
        keys = {row["key"] for row in integrations.integration_rows()}
        self.assertFalse({"alpaca", "goldapi", "ebay"} & keys)
        self.assertIn("telegram", keys)


@override_settings(BOT_API_REQUIRE_HTTPS=False)
class BotApiTests(TestCase):
    SCOPES = ["trading:notes", "reseller:read", "reseller:suggest", "notes:read"]

    def setUp(self):
        self.boss = User.objects.create_superuser("boss", "", PW)
        self.token, self.raw = BotToken.issue(self.boss, "Grok bot", self.SCOPES)
        self.api = Client()

    def get(self, path):
        return self.api.get(path, HTTP_AUTHORIZATION=f"Bearer {self.raw}")

    @ARCHIVED
    def test_archived_scopes_are_hidden_and_endpoints_404(self):
        from albright_bot_api import catalog, scopes
        self.assertFalse({"trading:notes", "reseller:read", "reseller:suggest"} & set(scopes.available(self.boss)))
        docs = self.get(reverse("bot_api_v1:docs")).content.decode()
        for hidden in ("reseller/lots", "trading/notes", "trading/strategies", "reseller/inventory"):
            self.assertNotIn(hidden, docs)
        spec = json.dumps(self.api.get(reverse("bot_api_v1:openapi")).json())
        self.assertNotIn("/reseller/lots", spec)
        self.assertNotIn("/trading/notes", spec)
        self.assertNotIn("getResellerLots", {t["name"] for t in catalog.tools(self.token)})
        self.assertFalse([t for t in catalog.tools() if t["name"] in {op.op_id for op in catalog.OPS
                                                                       if catalog.archived(op)}])
        for name in ("reseller_lots", "reseller_inventory", "trading_strategies", "trading_notes"):
            with self.subTest(endpoint=name):
                self.assertEqual(self.get(reverse(f"bot_api_v1:{name}")).status_code, 404)
        self.assertEqual(self.get(reverse("bot_api_v1:notes")).status_code, 200)   # other apps carry on
        # The key keeps its stored scopes, so restoring the app brings it back.
        self.token.refresh_from_db()
        self.assertEqual(set(self.token.scopes), set(self.SCOPES))
        with override_settings(ARCHIVED_APPS=[]):
            self.assertEqual(self.get(reverse("bot_api_v1:reseller_lots")).status_code, 200)

    @ARCHIVED
    def test_mcp_lists_no_archived_tools(self):
        from albright_bot_api import catalog
        response = self.api.post(reverse("bot_api_v1:mcp"), data=json.dumps({"jsonrpc": "2.0", "id": 1,
                                                                              "method": "tools/list"}),
                                 content_type="application/json", HTTP_ACCEPT="application/json, text/event-stream",
                                 HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        names = {t["name"] for t in response.json()["result"]["tools"]}
        self.assertFalse(names & {op.op_id for op in catalog.OPS if catalog.archived(op)})
        self.assertIn("listNotes", names)


class ResumeRemovedTests(TestCase):
    def test_resume_is_gone(self):
        for path in ("/resume/", "/resume/chat/", "/resume/anything/else"):
            with self.subTest(path=path):
                self.assertEqual(Client().get(path).status_code, 410)

    def test_no_resume_in_the_admin_nav(self):
        from albright_control import nav
        labels = [label for _key, _group, items in nav.GROUPS for label, _url, _patterns in items]
        self.assertNotIn("Resume", labels)
        client = Client()
        client.force_login(User.objects.create_superuser("boss", "", PW))
        content = client.get(reverse("albright_control:overview")).content.decode()
        self.assertNotIn("/control/resume/", content)
        self.assertNotIn(">Resume<", content)

    def test_export_writes_each_table_privately_then_nothing_once_dropped(self):
        out_dir = tempfile.mkdtemp()
        out = io.StringIO()
        call_command("export_resume_data", "--out", out_dir, stdout=out)      # 0004 ran in the test database
        self.assertIn("nothing to export", out.getvalue())
        with connection.cursor() as cursor:                                   # rolled back with the test
            cursor.execute("CREATE TABLE albright_resume_chatmessage (id integer, text text, created_at timestamptz)")
            cursor.execute("INSERT INTO albright_resume_chatmessage VALUES (1, 'hello', now())")
        out = io.StringIO()
        call_command("export_resume_data", "--out", out_dir, stdout=out)
        folder = next(Path(out_dir).glob("albright_resume-*"))
        rows = json.loads((folder / "albright_resume_chatmessage.json").read_text(encoding="utf-8"))
        self.assertEqual([r["text"] for r in rows], ["hello"])
        self.assertEqual(json.loads((folder / "manifest.json").read_text())["rows"], {"albright_resume_chatmessage": 1})
        with self.assertRaises(Exception):
            call_command("export_resume_data", "--out", str(Path(settings.STATIC_ROOT) / "x"), stdout=io.StringIO())
