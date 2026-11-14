"""Integration status for the Admin area: Configured / Missing / Failing.
Only ever reports WHETHER a key or token is set - never its value, not
even a prefix. Test buttons exist only where a test is cheap and safe
(Telegram test message, a test email to yourself);
there's deliberately no spot price test, since goldapi's monthly quota is
tiny. Each test takes the superuser who pressed it."""
import os
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from .models import IntegrationCheck, JobRun


def _env(*names):
    return all(os.environ.get(n) or getattr(settings, n, None) for n in names)


def _latest_check(name):
    return IntegrationCheck.objects.filter(integration=name).first()


def _recent_ai_failure():
    from albright_crypto_app.models import AIExtraction
    from albright_reselling_app.scanner_models import AIReview
    since = timezone.now() - timedelta(days=1)
    latest = [x for x in (AIReview.objects.filter(created_at__gte=since).order_by("-created_at").first(),
                          AIExtraction.objects.filter(created_at__gte=since).order_by("-created_at").first()) if x]
    newest = max(latest, key=lambda x: x.created_at, default=None)
    return newest is not None and newest.status == "error"


def integration_rows():
    from albright_trading_app.models import AlpacaCredentials
    spot_run = JobRun.objects.filter(job="fetch_spot_prices").exclude(ok=None).first()
    rows = [
        {"key": "openai", "name": "OpenAI", "used_for": "Reseller AI reviews, Meals recipe import, Opportunities extraction",
         "configured": _env("OPENAI_API_KEY"), "failing": _recent_ai_failure(),
         "failing_why": "the most recent AI call in the last 24h failed", "setting": "OPENAI_API_KEY"},
        {"key": "telegram", "name": "Telegram", "used_for": "Alerts and the daily digest",
         "configured": _env("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"), "test": True,
         "setting": "TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID"},
        {"key": "goldapi", "name": "goldapi.io", "used_for": "Daily silver/gold spot prices (90 calls/month)",
         "configured": _env("GOLDAPI_KEY"), "failing": bool(spot_run and not spot_run.ok),
         "failing_why": "the last fetch_spot_prices run failed", "setting": "GOLDAPI_KEY",
         "note": "No test button - every call uses the monthly quota."},
        {"key": "alpaca", "name": "Alpaca", "used_for": "Market data (site key) and each user's own trading account",
         "configured": _env("ALPACA_API_KEY"), "setting": "ALPACA_API_KEY",
         "note": f"{AlpacaCredentials.objects.count()} user account(s) connected (keys stored encrypted)"},
        {"key": "ebay", "name": "eBay", "used_for": "Reseller sold-price lookups",
         "configured": _env("EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET"), "setting": "EBAY_CLIENT_ID, EBAY_CLIENT_SECRET"},
        {"key": "email", "name": "Email", "used_for": "Invites and password setup/reset links (Gmail SMTP)",
         "configured": settings.EMAIL_CONFIGURED, "test": True,
         "setting": "EMAIL_HOST_USER, EMAIL_HOST_PASSWORD, DEFAULT_FROM_EMAIL",
         "note": "" if settings.EMAIL_CONFIGURED else "Without it, invite and password links are shown in Admin to send yourself."},
    ]
    for row in rows:
        check = _latest_check(row["key"])
        row["last_check"] = check
        failing = row.get("failing") or (check is not None and not check.ok)
        row["status"] = "missing" if not row["configured"] else "failing" if failing else "configured"
        if check is not None and not check.ok and not row.get("failing"):
            row["failing_why"] = f"last test failed: {check.message}"
    return rows


def test_telegram(user=None):
    from albright_reselling_app.scanner.alerts import send_telegram
    ok = send_telegram("<b>Albright Admin</b>\nTest message from the Integrations page.")
    return ok, "Test message sent." if ok else "Telegram didn't accept the message (token, chat ID or network)."


def test_email(user=None):
    from albright_labs import emails
    if not (user and user.email):
        return False, "Your account has no email address to send the test to."
    return emails.send("test", user.email, {})


TESTS = {"telegram": test_telegram, "email": test_email}
