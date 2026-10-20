"""Telegram alerts for the crypto app, sent with the reseller app's helper
(scanner.alerts.send_telegram - never raises, no-ops without a token):

  - one alert per new opportunity that isn't blocked or excluded, with
    confidence >= alert_min_confidence and EV/hour >= alert_min_ev_per_hour
  - one reminder 24 hours before the deadline of anything I'm doing
  - digest_lines(): the "Crypto" section of the daily digest

Links in messages go to this app's own page for the opportunity; the
listing itself is linked directly only when its domain is allowlisted, so
a tap from Telegram can't skip the "not on your verified list" warning.
"""
from datetime import timedelta
from decimal import Decimal
from html import escape

from django.db.models import Sum
from django.urls import reverse
from django.utils import timezone

from albright_reselling_app.scanner.alerts import SITE_BASE_URL, send_telegram

from . import config
from .core.safety import check_domain
from .models import Earning, Opportunity

REMINDER_WINDOW = timedelta(hours=24)


def _money(value):
    return f"${value:,.2f}" if value is not None else "?"


def _links(opp):
    page = SITE_BASE_URL + reverse("albright_crypto_app:opportunity", args=[opp.pk])
    links = [f'<a href="{escape(page)}">Open in app</a>']
    if check_domain(opp.url, config.allowlist())[1] == "allowlisted":
        links.append(f'<a href="{escape(opp.url)}">Listing</a>')
    return " · ".join(links)


def qualifies_for_alert(opp):
    min_rank = config.CONFIDENCE_RANK.get(config.get("alert_min_confidence"), 1)
    return (
        opp.safety_verdict != "block"
        and not opp.excluded
        and config.CONFIDENCE_RANK.get(opp.confidence, 0) >= min_rank
        and opp.ev_per_hour is not None
        and opp.ev_per_hour >= Decimal(str(config.get("alert_min_ev_per_hour")))
    )


def format_new_opportunity(opp):
    lines = [
        "<b>Crypto: new opportunity</b>",
        escape(opp.title[:150]),
        f"{opp.get_reward_type_display()} · reward {_money(opp.reward_usd)} · "
        f"EV {_money(opp.ev_mid)}/job ({_money(opp.ev_low)}–{_money(opp.ev_high)})",
        f"EV/hour {_money(opp.ev_per_hour)} · confidence {opp.confidence} · risk {opp.risk_score} ({opp.safety_verdict})",
    ]
    if opp.deadline:
        lines.append(f"Deadline: {timezone.localtime(opp.deadline):%b %d, %I:%M %p}")
    lines.append(_links(opp))
    return "\n".join(lines)


def send_new_opportunity_alerts(opportunities, now=None):
    """At most one alert per opportunity, ever (alert_sent_at)."""
    now = now or timezone.now()
    sent = []
    for opp in opportunities:
        if opp.alert_sent_at or not qualifies_for_alert(opp):
            continue
        claimed = Opportunity.objects.filter(pk=opp.pk, alert_sent_at__isnull=True).update(alert_sent_at=now)
        if claimed:
            send_telegram(format_new_opportunity(opp))
            sent.append(opp.pk)
    return sent


def send_deadline_reminders(now=None):
    """One reminder per opportunity I'm doing, once its deadline is within 24h."""
    now = now or timezone.now()
    due = Opportunity.objects.filter(status="doing", deadline__gt=now, deadline__lte=now + REMINDER_WINDOW,
                                     deadline_reminder_sent_at__isnull=True)
    sent = []
    for opp in due:
        claimed = Opportunity.objects.filter(pk=opp.pk, deadline_reminder_sent_at__isnull=True).update(
            deadline_reminder_sent_at=now)
        if not claimed:
            continue
        hours = max(0, int((opp.deadline - now).total_seconds() // 3600))
        send_telegram("\n".join([
            "<b>Crypto: deadline in under 24h</b>",
            escape(opp.title[:150]),
            f"Due {timezone.localtime(opp.deadline):%b %d, %I:%M %p} (about {hours}h left)",
            _links(opp),
        ]))
        sent.append(opp.pk)
    return sent


def digest_lines(now=None):
    """The daily digest's "Crypto" section: new opportunities (24h),
    deadlines coming up (7 days, things I'm doing or have submitted), and
    confirmed earnings this week."""
    now = now or timezone.now()
    new = Opportunity.objects.filter(discovered_at__gte=now - timedelta(hours=24))
    actionable = new.exclude(safety_verdict="block").filter(excluded=False)
    blocked = new.filter(safety_verdict="block").count()
    deadlines = Opportunity.objects.filter(status__in=("doing", "submitted"), deadline__gt=now,
                                           deadline__lte=now + timedelta(days=7)).order_by("deadline")
    week_start = timezone.localdate(now) - timedelta(days=6)
    earned = Earning.objects.filter(kind="confirmed", date__gte=week_start).aggregate(
        total=Sum("usd_value_at_receipt"))["total"] or Decimal("0")

    lines = ["<b>Crypto</b>",
             f"New (24h): {actionable.count()} actionable, {blocked} blocked"]
    best = actionable.exclude(ev_per_hour__isnull=True).order_by("-ev_per_hour")[:3]
    for opp in best:
        lines.append(f"  • {escape(opp.title[:70])} - {_money(opp.ev_per_hour)}/h ({opp.confidence})")
    if deadlines:
        lines.append("Deadlines (7 days):")
        for opp in deadlines[:5]:
            lines.append(f"  • {timezone.localtime(opp.deadline):%b %d} - {escape(opp.title[:70])} ({opp.status})")
    else:
        lines.append("Deadlines (7 days): none")
    lines.append(f"Earned this week (confirmed): {_money(earned)}")
    lines.append("")
    return lines
