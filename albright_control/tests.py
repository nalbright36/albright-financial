"""The Admin area (/control/): superuser-only (404 otherwise), header labels
and Admin visibility, Bot API moved here with password confirmation, the
site-suggestions endpoint/scope and inbox (prompt copy format), job health
logic, integrations never showing secrets, backups, and the audit log."""
import io
import json
import zipfile
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from albright_bot_api.models import BotRequestLog, BotToken, SiteSuggestion
from albright_control import jobs
from albright_control.models import AuditEntry, IntegrationCheck, JobRun
from albright_control.prompts import CLOSING, claude_code_prompt
from albright_control.urls import urlpatterns as control_urls

HOME_OUTLOOK = "albright_trading_app.views.get_market_outlook"
PASSWORD = "pw-not-real-12345"
PAGES = ["overview", "bot_api", "bot_activity", "suggestions", "health", "integrations", "ai_usage", "data", "audit"]
SECRETS = {"OPENAI_API_KEY": "sk-OPENAISECRET123", "TELEGRAM_BOT_TOKEN": "123:TELEGRAMSECRET", "TELEGRAM_CHAT_ID": "999",
           "GOLDAPI_KEY": "goldapi-SECRET", "GITHUB_TOKEN": "ghp_GITHUBSECRET", "EBAY_CLIENT_ID": "EBAYIDSECRET",
           "EBAY_CLIENT_SECRET": "EBAYSECRET"}


class AdminTestCase(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password=PASSWORD, email="")
        self.user = User.objects.create_user(username="regular", password=PASSWORD)
        self.client.force_login(self.admin)


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

class AccessTests(AdminTestCase):
    def all_urls(self):
        args = {"revoke_token": [1], "edit_token_scopes": [1], "rotate_token": [1], "idea": [1], "suggestion_to_idea": [1], "update_suggestion": [1], "test_integration": ["telegram"], "backup": ["ledger"]}
        return [(p.name, reverse(f"albright_control:{p.name}", args=args.get(p.name, []))) for p in control_urls]

    def test_every_admin_page_is_404_for_non_superusers(self):
        for who in ("anonymous", "regular", "staff"):
            client = Client()
            if who == "regular":
                client.force_login(self.user)
            elif who == "staff":
                staff = User.objects.create_user(username="staffer", password=PASSWORD, is_staff=True)
                client.force_login(staff)
            for name, url in self.all_urls():
                with self.subTest(who=who, page=name):
                    self.assertEqual(client.get(url).status_code, 404)
                    self.assertEqual(client.post(url, {"password": PASSWORD}).status_code, 404)
        self.assertFalse(AuditEntry.objects.exists())

    def test_superuser_sees_every_page(self):
        for name in PAGES:
            with self.subTest(page=name):
                response = self.client.get(reverse(f"albright_control:{name}"))
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, '<body data-app="admin">')
                self.assertContains(response, 'class="navbar__brand">Albright Admin</a>')

    def test_admin_nav(self):
        """Every Admin page is in the nav (the groups themselves: test_header_nav)."""
        content = self.client.get(reverse("albright_control:overview")).content.decode()
        nav = content.split('id="navbar-links"', 1)[1].split('class="navbar__dropdown navbar__account"', 1)[0]
        labels = [label for label in ("Overview", "Bot API", "Bot suggestions", "System health", "Integrations",
                                      "AI usage", "Data", "Audit log") if f">{label}</" in nav]
        self.assertEqual(len(labels), 8)


@mock.patch(HOME_OUTLOOK, return_value=None)
class HeaderTests(AdminTestCase):
    def buttons(self, content):
        from albright_labs.testing import launcher_tiles
        return launcher_tiles(content)

    def test_short_labels_and_admin_only_for_superusers(self, _outlook):
        page = self.client.get(reverse("trading_home")).content.decode()
        self.assertEqual(self.buttons(page), [("labs", "Home"), ("trading", "Trade"), ("reselling", "Resell"),
                                              ("opportunities", "Opps"), ("meals", "Meals"), ("admin", "Admin")])
        self.assertIn('title="Albright Trading"', page)
        regular = Client()
        regular.force_login(self.user)
        page = regular.get(reverse("trading_home")).content.decode()
        self.assertEqual([t for t, _l in self.buttons(page)], ["labs", "trading", "reselling", "opportunities", "meals"])
        self.assertNotIn(reverse("albright_control:overview"), page)

    def test_brand_names_stay_full(self, _outlook):
        for url, brand in ((reverse("trading_home"), "Albright Trading"),
                           (reverse("albright_reselling_app:dashboard"), "Albright Reselling"),
                           (reverse("albright_crypto_app:inbox"), "Albright Opportunities"),
                           (reverse("albright_control:health"), "Albright Admin")):
            with self.subTest(url=url):
                content = self.client.get(url).content.decode()
                self.assertIn(f'class="navbar__brand">{brand}</a>', content)
        admin = self.client.get(reverse("albright_control:health")).content.decode()
        self.assertIn("<title>System health · Albright Admin</title>", admin)
        self.assertIn('data-app-target="admin" title="Albright Admin" aria-current="page"', admin)

    def test_admin_accent_is_slate_and_semantic_colors_untouched(self, _outlook):
        css = (settings.BASE_DIR / "albright_trading_app" / "static" / "style.css").read_text(encoding="utf-8") \
            if hasattr(settings.BASE_DIR, "joinpath") else open(f"{settings.BASE_DIR}/albright_trading_app/static/style.css", encoding="utf-8").read()
        block = css.split('\nbody[data-app="admin"] {', 1)[1].split("}", 1)[0]
        self.assertIn("--accent-rgb: 100, 116, 139;", block)
        self.assertIn("--accent: #94A3B8;", block)
        self.assertIn("--accent: #475569;", css.split('[data-theme="light"] body[data-app="admin"] {', 1)[1].split("}", 1)[0])
        self.assertIn("--gain: var(--emerald);", css)
        self.assertIn('.app-launcher__tile[data-app-target="admin"] { --app-dot: var(--app-admin); }', css)


# ---------------------------------------------------------------------------
# Bot API in Admin
# ---------------------------------------------------------------------------

class BotApiAdminTests(AdminTestCase):
    def test_old_url_redirects(self):
        response = self.client.get("/bot-api/")
        self.assertRedirects(response, reverse("albright_control:bot_api"), status_code=301)

    def test_creating_a_token_needs_my_password(self):
        response = self.client.post(reverse("albright_control:bot_api"),
                                    {"name": "Grok", "scopes": ["site:suggest"], "password": "wrong"})
        self.assertContains(response, "That isn&#x27;t your password - no token was created.")
        self.assertFalse(BotToken.objects.exists())
        response = self.client.post(reverse("albright_control:bot_api"),
                                    {"name": "Grok", "scopes": ["site:suggest"], "password": PASSWORD})
        self.assertTrue(response.context["new_token"].startswith("abt_"))
        self.assertEqual(BotToken.objects.get().scopes, ["site:suggest"])
        entry = AuditEntry.objects.get()
        self.assertEqual(entry.action, "token_created")
        self.assertIn('"Grok"', entry.details)
        self.assertNotIn(response.context["new_token"], entry.details)

    def test_revoking_needs_my_password(self):
        token, _raw = BotToken.issue(self.admin, "Grok", ["site:suggest"])
        self.client.post(reverse("albright_control:revoke_token", args=[token.pk]), {"password": "nope"})
        token.refresh_from_db()
        self.assertTrue(token.is_active)
        self.assertFalse(AuditEntry.objects.exists())
        self.client.post(reverse("albright_control:revoke_token", args=[token.pk]), {"password": PASSWORD})
        token.refresh_from_db()
        self.assertFalse(token.is_active)
        self.assertEqual(AuditEntry.objects.get().action, "token_revoked")


# ---------------------------------------------------------------------------
# Site suggestions
# ---------------------------------------------------------------------------

@override_settings(BOT_API_REQUIRE_HTTPS=False)
class SiteSuggestionTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.token, self.raw = BotToken.issue(self.admin, "Grok", ["site:suggest"])
        self.api = Client()

    def post(self, body, raw=None):
        return self.api.post(reverse("bot_api_v1:site_suggestions"), data=json.dumps(body),
                             content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw or self.raw}")

    GOOD = {"app": "resell", "area": "Watchlist page", "title": "Show time left in red under 10 minutes",
            "description": "Lots ending soon are easy to miss. <b>Color</b> the countdown red.", "priority": "high"}

    def test_endpoint_and_scope(self):
        response = self.post(self.GOOD)
        self.assertEqual((response.status_code, response.json()["status"]), (201, "new"))
        s = SiteSuggestion.objects.get()
        self.assertEqual((s.app, s.area, s.priority, s.status, s.token), ("resell", "Watchlist page", "high", "new", self.token))
        _other, other_raw = BotToken.issue(self.admin, "No scope", ["reseller:read"])
        self.assertEqual(self.post(self.GOOD, raw=other_raw).status_code, 403)
        for bad in ({**self.GOOD, "app": "crypto"}, {**self.GOOD, "priority": "urgent"}, {**self.GOOD, "title": ""},
                    {**self.GOOD, "description": "x" * 5001}):
            self.assertEqual(self.post(bad).status_code, 400)
        self.assertEqual(self.post({k: v for k, v in self.GOOD.items() if k not in ("area", "priority")}).status_code, 201)
        self.assertEqual(SiteSuggestion.objects.order_by("-pk").first().priority, "medium")

    def test_docs_mention_it(self):
        text = self.api.get(reverse("bot_api_v1:docs"), HTTP_AUTHORIZATION=f"Bearer {self.raw}").content.decode()
        self.assertIn("POST site-suggestions", text)
        self.assertIn("site:suggest", text)
        self.assertIn('"trade" | "resell" | "opps" | "all"', text)

    def test_inbox_filters_status_notes_and_escaping(self):
        self.post(self.GOOD)
        self.post({**self.GOOD, "app": "opps", "title": "Opps idea", "description": "Add a calendar view."})
        page = self.client.get(reverse("albright_control:suggestions"))
        self.assertContains(page, "Show time left in red")
        self.assertContains(page, "&lt;b&gt;Color&lt;/b&gt;")
        self.assertNotContains(page, "<b>Color</b>")
        filtered = self.client.get(reverse("albright_control:suggestions"), {"app": "opps"})
        self.assertContains(filtered, "Opps idea")
        self.assertNotContains(filtered, "Show time left in red")

        s = SiteSuggestion.objects.get(app="resell")
        self.assertNotContains(page, "Copy as Claude Code prompt")   # only accepted ones
        self.client.post(reverse("albright_control:update_suggestion", args=[s.pk]),
                         {f"s{s.pk}-status": "accepted", f"s{s.pk}-my_notes": "Use the existing warn color."})
        s.refresh_from_db()
        self.assertEqual((s.status, s.my_notes), ("accepted", "Use the existing warn color."))
        self.assertEqual(AuditEntry.objects.get().action, "suggestion_status")
        accepted = self.client.get(reverse("albright_control:suggestions"), {"status": "accepted"})
        self.assertContains(accepted, f'data-copy-target="prompt-{s.pk}">Copy as Claude Code prompt</button>')
        self.assertNotContains(accepted, "Opps idea")
        # Notes-only change: no audit entry for status.
        self.client.post(reverse("albright_control:update_suggestion", args=[s.pk]),
                         {f"s{s.pk}-status": "accepted", f"s{s.pk}-my_notes": "changed"})
        self.assertEqual(AuditEntry.objects.count(), 1)

    def test_prompt_copy_format(self):
        s = SiteSuggestion.objects.create(app="resell", area="Watchlist page", title="Red countdown",
                                          description="Color the countdown red under 10 minutes.", priority="high",
                                          status="accepted", my_notes="Use --loss.")
        prompt = claude_code_prompt(s)
        self.assertEqual(prompt, "Red countdown\n\n"
                                 "App: The Reselling app (albright_reselling_app).\n"
                                 "Area: Watchlist page\n"
                                 "Priority: high\n\n"
                                 "Color the countdown red under 10 minutes.\n\n"
                                 "My notes:\nUse --loss.\n\n" + CLOSING)
        self.assertIn("python manage.py check and python manage.py test --keepdb", CLOSING)
        self.assertIn("makemigrations and migrate", CLOSING)
        self.assertIn("showmigrations shows nothing unapplied", CLOSING)


# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------

class HealthTests(AdminTestCase):
    def test_status_logic(self):
        now = timezone.now()
        hour = timedelta(hours=1)
        self.assertEqual(jobs.job_status(True, now - 30 * timedelta(minutes=1), hour, now), "ok")
        self.assertEqual(jobs.job_status(True, now - hour * 1.4, hour, now), "ok")
        self.assertEqual(jobs.job_status(True, now - hour * 1.6, hour, now), "late")
        self.assertEqual(jobs.job_status(None, None, hour, now), "late")          # never ran
        self.assertEqual(jobs.job_status(False, now - timedelta(minutes=5), hour, now), "failing")
        day = timedelta(days=1)
        self.assertEqual(jobs.job_status(True, now - timedelta(hours=30), day, now), "ok")
        self.assertEqual(jobs.job_status(True, now - timedelta(hours=40), day, now), "late")

    def test_summarize_uses_the_newest_finished_run(self):
        now = timezone.now()
        events = [(now - timedelta(hours=3), True, ""), (now - timedelta(hours=1), False, "Timeout"),
                  (now - timedelta(hours=2), True, "")]
        row = jobs.summarize("x", "X", timedelta(hours=1), events, now=now)
        self.assertEqual((row["status"], row["last_error"], row["last_success"]), ("failing", "Timeout", events[2][0]))
        recovered = jobs.summarize("x", "X", timedelta(hours=1), events + [(now - timedelta(minutes=5), True, "")], now=now)
        self.assertEqual(recovered["status"], "ok")
        self.assertEqual(recovered["last_error"], "Timeout")    # still shown, for reference

    def test_tracked_job_records_success_and_failure(self):
        class Good(BaseCommand):
            @jobs.tracked_job("calibrate")
            def handle(self, *args, **opts):
                return None

        class Bad(BaseCommand):
            @jobs.tracked_job(lambda opts: f"scan_lots:{opts['source']}")
            def handle(self, *args, **opts):
                raise RuntimeError("site down")

        Good().handle()
        with self.assertRaises(RuntimeError):
            Bad().handle(source="hibid")
        Good().handle(dry_run=True)   # dry runs aren't recorded
        runs = {r.job: r for r in JobRun.objects.all()}
        self.assertEqual(set(runs), {"calibrate", "scan_lots:hibid"})
        self.assertTrue(runs["calibrate"].ok)
        self.assertEqual((runs["scan_lots:hibid"].ok, runs["scan_lots:hibid"].error), (False, "RuntimeError: site down"))
        self.assertIsNotNone(runs["calibrate"].finished_at)

    def test_health_page_rows_per_job(self):
        from albright_crypto_app.models import Source
        from albright_reselling_app.scanner_models import ScanRun
        now = timezone.now()
        JobRun.objects.create(job="watchlist_alerts", ok=True, finished_at=now)
        JobRun.objects.create(job="alert_digest", ok=False, error="Telegram down", finished_at=now)
        run = ScanRun.objects.create(source="hibid", error="")
        ScanRun.objects.filter(pk=run.pk).update(finished_at=now)
        Source.objects.create(kind="rss", name="Feed", url="https://example.org/feed", last_checked=now)
        Source.objects.create(kind="remotive", name="Remotive", url="https://remotive.com/api/remote-jobs",
                              opportunity_type="income")
        rows = {r["job"]: r for r in self.client.get(reverse("albright_control:health")).context["rows"]}
        for source in settings.RESELLING_SCANNER["SOURCES"]:
            self.assertIn(f"scan_lots:{source}", rows)
        for job in ("watchlist_alerts", "track_closed", "fetch_spot_prices", "alert_digest", "calibrate", "prune_lots", "gmail"):
            self.assertIn(job, rows)
        self.assertEqual(rows["watchlist_alerts"]["status"], "ok")
        self.assertEqual((rows["alert_digest"]["status"], rows["alert_digest"]["last_error"]), ("failing", "Telegram down"))
        self.assertEqual(rows["calibrate"]["status"], "late")      # never ran
        self.assertEqual(rows["scan_lots:hibid"]["status"], "ok")
        crypto = [r for k, r in rows.items() if k.startswith("scan_crypto:")]
        self.assertEqual({r["label"]: r["status"] for r in crypto},
                         {"Opportunities scan - Feed": "ok", "Opportunities scan - Remotive": "off"})
        self.assertEqual(rows["gmail"]["status"], "off")


# ---------------------------------------------------------------------------
# Integrations
# ---------------------------------------------------------------------------

class IntegrationTests(AdminTestCase):
    def test_never_renders_secret_values(self):
        with mock.patch.dict("os.environ", SECRETS):
            response = self.client.get(reverse("albright_control:integrations"))
            overview = self.client.get(reverse("albright_control:overview"))
        content = response.content.decode() + overview.content.decode()
        for value in SECRETS.values():
            self.assertNotIn(value, content)
        self.assertNotIn("SECRET", content.replace("_SECRET", ""))   # every fake secret contains SECRET
        statuses = {r["key"]: r["status"] for r in response.context["rows"]}
        self.assertEqual(statuses["telegram"], "configured")
        self.assertEqual(statuses["ebay"], "configured")
        self.assertEqual(set(statuses), {"openai", "telegram", "goldapi", "alpaca", "gmail", "github", "ebay"})

    def test_missing_and_failing(self):
        with mock.patch.dict("os.environ", {}, clear=False):
            for name in SECRETS:
                mock.patch.dict("os.environ").start()
            import os
            saved = {k: os.environ.pop(k, None) for k in SECRETS}
            try:
                statuses = {r["key"]: r["status"] for r in self.client.get(reverse("albright_control:integrations")).context["rows"]}
            finally:
                for k, v in saved.items():
                    if v is not None:
                        os.environ[k] = v
                mock.patch.stopall()
        self.assertEqual(statuses["telegram"], "missing")
        self.assertEqual(statuses["goldapi"], "missing")
        with mock.patch.dict("os.environ", SECRETS):
            JobRun.objects.create(job="fetch_spot_prices", ok=False, error="401", finished_at=timezone.now())
            IntegrationCheck.objects.create(integration="telegram", ok=False, message="chat not found")
            rows = {r["key"]: r for r in self.client.get(reverse("albright_control:integrations")).context["rows"]}
        self.assertEqual((rows["goldapi"]["status"], rows["telegram"]["status"]), ("failing", "failing"))
        self.assertIn("chat not found", rows["telegram"]["failing_why"])

    def test_telegram_test_button_and_audit(self):
        with mock.patch.dict("os.environ", SECRETS), \
                mock.patch("albright_reselling_app.scanner.alerts.send_telegram", return_value=True) as send:
            self.client.post(reverse("albright_control:test_integration", args=["telegram"]))
        send.assert_called_once()
        self.assertTrue(IntegrationCheck.objects.get(integration="telegram").ok)
        self.assertEqual(AuditEntry.objects.get().action, "integration_test")

    def test_gmail_check_and_no_spot_price_test(self):
        self.client.post(reverse("albright_control:test_integration", args=["gmail"]))
        check = IntegrationCheck.objects.get(integration="gmail")
        self.assertEqual((check.ok, check.message), (False, "Gmail isn't connected."))
        with mock.patch("albright_reselling_app.management.commands.fetch_spot_prices.Command.handle") as fetch:
            response = self.client.post(reverse("albright_control:test_integration", args=["goldapi"]))
        fetch.assert_not_called()
        self.assertEqual(response.status_code, 302)
        self.assertFalse(IntegrationCheck.objects.filter(integration="goldapi").exists())


# ---------------------------------------------------------------------------
# AI usage, data, audit
# ---------------------------------------------------------------------------

class AiUsageTests(AdminTestCase):
    def test_spend_by_day_and_feature_with_limits(self):
        from albright_crypto_app.models import AIExtraction
        from albright_reselling_app.scanner_models import AIReview, SourcedLot
        lot = SourcedLot.objects.create(source="shopgoodwill", external_id="ai1", url="https://example.com/1", title="x",
                                        current_price=Decimal("1"))
        AIReview.objects.create(lot=lot, cost_usd=Decimal("0.0300"))
        AIExtraction.objects.create(purpose="walkthrough", cost_usd=Decimal("0.0100"))
        AIExtraction.objects.create(purpose="proposal", cost_usd=Decimal("0.0050"))
        AIExtraction.objects.create(purpose="extract", cost_usd=Decimal("0.0020"))
        usage = self.client.get(reverse("albright_control:ai_usage")).context["usage"]
        by_feature = {f["key"]: (f["cost"], f["calls"]) for f in usage["by_feature"]}
        self.assertEqual(by_feature["reseller_review"], (Decimal("0.0300"), 1))
        self.assertEqual(by_feature["walkthrough"], (Decimal("0.0100"), 1))
        self.assertEqual(usage["total"], Decimal("0.0470"))
        self.assertEqual(len(usage["by_day"]), 1)
        self.assertEqual(usage["by_day"][0]["total"], Decimal("0.0470"))
        limits = [l["label"] for l in usage["limits"]]
        self.assertIn("Reseller AI reviews per day", limits)
        self.assertIn("Opportunities AI calls per day", limits)
        self.assertEqual(usage["limits"][1]["used"], 1)
        self.assertEqual(usage["limits"][2]["used"], 3)


class DataTests(AdminTestCase):
    def make_data(self):
        from albright_crypto_app.models import Earning
        from albright_crypto_app.tests.test_pipeline import make
        from albright_reselling_app.scanner_models import BidWatch, LotEvaluation, SourcedLot
        from albright_reselling_app.tests import test_expenses as te, test_ledger as tl, test_selling as ts
        entry = tl._make_entry(self.admin, item="Morgan dollar", cost="22.00")
        ts._make_sale(entry, sale_price="35.00")
        te._make_expense(self.admin, description="Bubble mailers")
        lot = SourcedLot.objects.create(source="hibid", external_id="c1", url="https://example.com/c1", title="Closed lot",
                                        current_price=Decimal("12"), final_price=Decimal("15"),
                                        features={"photo_count": 3, "is_relisted": False})
        LotEvaluation.objects.create(lot=lot, max_bid=Decimal("20"), headroom=Decimal("5"), category="coins")
        SourcedLot.objects.create(source="hibid", external_id="o1", url="https://example.com/o1", title="Still open",
                                  current_price=Decimal("3"))
        BidWatch.objects.create(lot=lot, my_max_bid=Decimal("18"), status="likely_won", archived_at=timezone.now(),
                                archive_reason="closed_on_watchlist", final_price=Decimal("15"))
        opp = make(title="Bounty")
        Earning.objects.create(opportunity=opp, amount=Decimal("50"), currency="USDC", usd_value_at_receipt=Decimal("50"))

    def read_zip(self, key):
        response = self.client.get(reverse("albright_control:backup", args=[key]))
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertIn(f'albright-{key}-', response["Content-Disposition"])
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        return {name: archive.read(name).decode() for name in archive.namelist()}

    def test_backup_zip_contents(self):
        self.make_data()
        ledger = self.read_zip("ledger")
        self.assertEqual(set(ledger), {"ledger_entries.csv", "ledger_sales.csv"})
        self.assertIn("Morgan dollar", ledger["ledger_entries.csv"])
        self.assertIn("boss", ledger["ledger_entries.csv"])
        self.assertIn("35.00", ledger["ledger_sales.csv"])
        self.assertIn("Bubble mailers", self.read_zip("expenses")["expenses.csv"])
        closed = self.read_zip("closed-lots")["closed_lots.csv"]
        header = closed.splitlines()[0]
        self.assertIn("feature_photo_count", header)
        self.assertIn("eval_max_bid", header)
        self.assertIn("Closed lot", closed)
        self.assertNotIn("Still open", closed)
        self.assertIn("likely_won", self.read_zip("watchlist-archive")["watchlist_archive.csv"])
        opps = self.read_zip("opportunities")
        self.assertEqual(set(opps), {"opportunities.csv", "earnings.csv"})
        self.assertIn("Bounty", opps["opportunities.csv"])
        self.assertNotIn("raw_text", opps["opportunities.csv"].splitlines()[0])
        self.assertIn("USDC", opps["earnings.csv"])
        downloads = AuditEntry.objects.filter(action="backup_download")
        self.assertEqual(downloads.count(), 5)
        self.assertIn("ledger_entries.csv (1 rows)", downloads.order_by("pk").first().details)

    def test_row_counts_and_prune_dry_run(self):
        self.make_data()
        response = self.client.get(reverse("albright_control:data"))
        counts = {r["label"]: r["count"] for g in response.context["groups"] for r in g["rows"]}
        self.assertEqual(counts["albright_reselling_app.SourcedLot"], 2)
        self.assertEqual(counts["albright_reselling_app.LedgerEntry"], 1)
        from albright_reselling_app.scanner_models import LotEvaluation, SourcedLot
        stale = SourcedLot.objects.create(source="hibid", external_id="stale", url="https://example.com/s",
                                          title="Never matched", current_price=Decimal("1"))
        LotEvaluation.objects.create(lot=stale, category="none")
        SourcedLot.objects.filter(pk=stale.pk).update(last_seen=timezone.now() - timedelta(days=45))
        before = SourcedLot.objects.count()
        response = self.client.post(reverse("albright_control:data"), {"action": "prune_dry_run"})
        self.assertContains(response, 'id="prune-output"')
        self.assertIn("Would delete", response.context["dry_run"])
        self.assertEqual(SourcedLot.objects.count(), before)          # dry run: nothing deleted
        self.assertFalse(JobRun.objects.filter(job="prune_lots").exists())
        self.assertEqual(AuditEntry.objects.get().action, "prune_dry_run")

    def test_unknown_backup_is_404(self):
        self.assertEqual(self.client.get("/control/data/backup/passwords.zip").status_code, 404)


class AuditLogTests(AdminTestCase):
    def test_entries_and_filter(self):
        token, _raw = BotToken.issue(self.admin, "Grok", ["site:suggest"])
        self.client.post(reverse("albright_control:revoke_token", args=[token.pk]), {"password": PASSWORD})
        self.client.get(reverse("albright_control:backup", args=["expenses"]))
        page = self.client.get(reverse("albright_control:audit"))
        self.assertContains(page, "token_revoked")
        self.assertContains(page, "backup_download")
        self.assertContains(page, "boss")
        filtered = self.client.get(reverse("albright_control:audit"), {"action": "token_revoked"})
        self.assertContains(filtered, "&quot;Grok&quot;")
        self.assertNotContains(filtered, "albright-expenses-")


# ---------------------------------------------------------------------------
# Token management: edit permissions, rotate
# ---------------------------------------------------------------------------

@override_settings(BOT_API_REQUIRE_HTTPS=False)
class TokenManagementTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        self.token, self.raw = BotToken.issue(self.admin, "Grok bot", ["reseller:read"])
        self.api = Client()

    def suggest(self, raw):
        return self.api.post(reverse("bot_api_v1:site_suggestions"),
                             data=json.dumps({"app": "all", "title": "Idea", "description": "x"}),
                             content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}")

    def test_create_form_lists_every_scope_with_a_description(self):
        from django.utils.html import escape
        from albright_bot_api.models import SCOPES
        page = self.client.get(reverse("albright_control:bot_api")).content.decode()
        form = page.split('id="token-form"', 1)[1].split("</form>", 1)[0]
        self.assertEqual(len(SCOPES), 10)
        for key, description in SCOPES:
            self.assertIn(f'value="{key}"', form)
            self.assertIn(f"<code>{key}</code> &mdash; {escape(description)}", form)

    def test_edit_permissions_takes_effect_immediately_without_changing_the_token(self):
        self.assertEqual(self.suggest(self.raw).status_code, 403)
        page = self.client.get(reverse("albright_control:bot_api")).content.decode()
        edit = page.split(f'id="edit-token-{self.token.pk}"', 1)[1].split("</details>", 1)[0]
        self.assertIn('value="reseller:read" checked', edit)       # pre-filled with its current scopes
        self.assertNotIn('value="site:suggest" checked', edit)
        hash_before = BotToken.objects.get(pk=self.token.pk).token_hash

        self.client.post(reverse("albright_control:edit_token_scopes", args=[self.token.pk]),
                         {"scopes": ["reseller:read", "site:suggest"], "password": PASSWORD})

        token = BotToken.objects.get(pk=self.token.pk)
        self.assertEqual(token.scopes, ["reseller:read", "site:suggest"])
        self.assertEqual(token.token_hash, hash_before)            # same token value
        self.assertEqual(self.suggest(self.raw).status_code, 201)  # works right away
        entry = AuditEntry.objects.get()
        self.assertEqual(entry.action, "token_scopes_changed")
        self.assertIn("reseller:read -> reseller:read, site:suggest", entry.details)

        self.client.post(reverse("albright_control:edit_token_scopes", args=[self.token.pk]),
                         {"scopes": ["site:suggest"], "password": PASSWORD})
        reader = self.api.get(reverse("bot_api_v1:reseller_lots"), HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        self.assertEqual(reader.status_code, 403)                  # a removed scope stops working at once

    def test_edit_permissions_needs_my_password(self):
        self.client.post(reverse("albright_control:edit_token_scopes", args=[self.token.pk]),
                         {"scopes": ["site:suggest"], "password": "wrong"})
        self.assertEqual(BotToken.objects.get(pk=self.token.pk).scopes, ["reseller:read"])
        self.assertEqual(self.suggest(self.raw).status_code, 403)
        self.assertFalse(AuditEntry.objects.exists())

    def test_rotate_invalidates_the_old_value_and_shows_the_new_one_once(self):
        response = self.client.post(reverse("albright_control:rotate_token", args=[self.token.pk]), {"password": PASSWORD})
        new_raw = response.context["new_token"]
        self.assertNotEqual(new_raw, self.raw)
        self.assertContains(response, new_raw)
        self.assertContains(response, 'data-copy-target="new-token-value"')
        token = BotToken.objects.get(pk=self.token.pk)
        self.assertEqual((token.name, token.scopes, token.is_active), ("Grok bot", ["reseller:read"], True))
        self.assertEqual(BotToken.objects.count(), 1)
        old = self.api.get(reverse("bot_api_v1:docs"), HTTP_AUTHORIZATION=f"Bearer {self.raw}")
        new = self.api.get(reverse("bot_api_v1:docs"), HTTP_AUTHORIZATION=f"Bearer {new_raw}")
        self.assertEqual((old.status_code, new.status_code), (401, 200))
        self.assertNotContains(self.client.get(reverse("albright_control:bot_api")), new_raw)   # shown once
        entry = AuditEntry.objects.get()
        self.assertEqual(entry.action, "token_rotated")
        self.assertNotIn(new_raw, entry.details)

    def test_rotate_needs_my_password_and_asks_to_confirm(self):
        self.client.post(reverse("albright_control:rotate_token", args=[self.token.pk]), {"password": "wrong"})
        self.assertEqual(self.api.get(reverse("bot_api_v1:docs"), HTTP_AUTHORIZATION=f"Bearer {self.raw}").status_code, 200)
        self.assertFalse(AuditEntry.objects.exists())
        page = self.client.get(reverse("albright_control:bot_api")).content.decode()
        self.assertIn('data-confirm="Rotate Grok bot? The current key stops working immediately', page)

    def test_revoked_tokens_cant_be_edited_or_rotated(self):
        self.token.revoke()
        for name in ("edit_token_scopes", "rotate_token"):
            response = self.client.post(reverse(f"albright_control:{name}", args=[self.token.pk]),
                                        {"password": PASSWORD, "scopes": ["site:suggest"]})
            self.assertEqual(response.status_code, 404)

    def test_non_superusers_get_404(self):
        client = Client()
        client.force_login(self.user)
        for name in ("edit_token_scopes", "rotate_token"):
            self.assertEqual(client.post(reverse(f"albright_control:{name}", args=[self.token.pk]),
                                         {"password": PASSWORD}).status_code, 404)


# ---------------------------------------------------------------------------
# Bot notes cleanup (Grok Bot page)
# ---------------------------------------------------------------------------

@mock.patch(HOME_OUTLOOK, return_value=None)
class BotNotesCleanupTests(AdminTestCase):
    def setUp(self):
        super().setUp()
        from albright_bot_api.models import TradingNote
        self.token, _raw = BotToken.issue(self.admin, "Grok bot", ["trading:notes"])
        today = timezone.localdate()
        self.notes = [TradingNote.objects.create(owner=self.admin, token=self.token, date=today, title=f"Note {i}",
                                                 text=f"Idea number {i}") for i in range(3)]
        self.others = TradingNote.objects.create(owner=self.user, date=today, title="Not mine", text="x")

    def page(self):
        return self.client.get(reverse("grok_bot")).content.decode()

    def test_checkboxes_select_all_and_buttons(self, _outlook):
        page = self.page()
        self.assertIn('data-select-all="note"', page)
        for note in self.notes:
            self.assertIn(f'name="note" value="{note.pk}"', page)
            self.assertIn(reverse("albright_bot_api:trading_note_delete", args=[note.pk]), page)
        self.assertIn('name="action" value="move"', page)
        self.assertIn('data-confirm="Delete the selected notes?', page)
        self.assertNotIn("Not mine", page)

    def test_bulk_delete(self, _outlook):
        from albright_bot_api.models import TradingNote
        self.client.post(reverse("albright_bot_api:trading_notes_bulk"),
                         {"action": "delete", "note": [self.notes[0].pk, self.notes[1].pk, self.others.pk]})
        self.assertEqual(list(TradingNote.objects.filter(owner=self.admin)), [self.notes[2]])
        self.assertTrue(TradingNote.objects.filter(pk=self.others.pk).exists())   # never someone else's
        entry = AuditEntry.objects.get()
        self.assertEqual(entry.action, "bot_notes_deleted")
        self.assertIn('2 note(s): "Note 0"; "Note 1"', entry.details)

    def test_move_to_bot_suggestions(self, _outlook):
        from albright_bot_api.models import TradingNote
        response = self.client.post(reverse("albright_bot_api:trading_notes_bulk"),
                                    {"action": "move", "note": [self.notes[1].pk, self.notes[2].pk]})
        self.assertRedirects(response, reverse("grok_bot") + "#bot-notes", fetch_redirect_response=False)
        moved = SiteSuggestion.objects.order_by("pk")
        self.assertEqual([(s.title, s.description, s.app, s.priority, s.status, s.token) for s in moved],
                         [("Note 1", "Idea number 1", "all", "medium", "new", self.token),
                          ("Note 2", "Idea number 2", "all", "medium", "new", self.token)])
        self.assertEqual(list(TradingNote.objects.filter(owner=self.admin)), [self.notes[0]])   # removed from Trading
        self.assertEqual(AuditEntry.objects.get().action, "bot_notes_moved")
        inbox = self.client.get(reverse("albright_control:suggestions"), {"status": "new"})
        self.assertContains(inbox, "Idea number 1")

    def test_single_delete(self, _outlook):
        from albright_bot_api.models import TradingNote
        self.client.post(reverse("albright_bot_api:trading_note_delete", args=[self.notes[0].pk]),
                         {"note": [self.notes[1].pk]})   # other ticked boxes are ignored
        self.assertEqual(TradingNote.objects.filter(owner=self.admin).count(), 2)
        self.assertFalse(TradingNote.objects.filter(pk=self.notes[0].pk).exists())
        self.assertEqual(AuditEntry.objects.get().action, "bot_notes_deleted")
        self.assertEqual(self.client.post(reverse("albright_bot_api:trading_note_delete",
                                                  args=[self.others.pk])).status_code, 404)

    def test_nothing_selected_does_nothing(self, _outlook):
        from albright_bot_api.models import TradingNote
        self.client.post(reverse("albright_bot_api:trading_notes_bulk"), {"action": "delete"})
        self.assertEqual(TradingNote.objects.count(), 4)
        self.assertFalse(AuditEntry.objects.exists())
