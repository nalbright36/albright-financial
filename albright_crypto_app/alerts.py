"""Telegram alerts for the Opportunities app, sent with the reseller app's
helper (scanner.alerts.send_telegram - never raises, no-ops without a token):

  - one alert per new opportunity (crypto or income) that isn't blocked or
    excluded, with confidence >= alert_min_confidence and EV/hour >=
    alert_min_ev_per_hour - and, for freelance work, skill_match >=
    alert_min_skill_match
  - one reminder 24 hours before the deadline of anything I'm doing
  - digest_lines(): the "Opportunities" section of the daily digest, with a
    Crypto and an Income line

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
from .models import OPPORTUNITY_TYPES, Earning, Opportunity, Source

REMINDER_WINDOW = timedelta(hours=24)
TYPE_LABELS = dict(OPPORTUNITY_TYPES)


def _money(value):
    return f"${value:,.2f}" if value is not None else "?"


def _title(opp, length):
    """The opportunity's title for Telegram - except for jobs from Remotive /
    Remote OK, which never leave the app: just where it's from."""
    if opp.source.keep_in_app:
        return f"A job via {escape(opp.source.attribution[0])} (details in the app)"
    return escape(opp.title[:length])


def _links(opp):
    page = SITE_BASE_URL + reverse("albright_crypto_app:opportunity", args=[opp.pk])
    links = [f'<a href="{escape(page)}">Open in app</a>']
    if not opp.source.keep_in_app and check_domain(opp.url, config.allowlist())[1] == "allowlisted":
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
        and (opp.reward_type != "freelance"
             or (opp.skill_match is not None and opp.skill_match >= int(config.get("alert_min_skill_match"))))
    )


def format_new_opportunity(opp):
    lines = [
        f"<b>{TYPE_LABELS[opp.opportunity_type]}: new opportunity</b>",
        escape(opp.title[:150]),
        f"{opp.get_reward_type_display()} · reward {_money(opp.reward_usd)} · "
        f"EV {_money(opp.ev_mid)}/job ({_money(opp.ev_low)}–{_money(opp.ev_high)})",
        f"EV/hour {_money(opp.ev_per_hour)} · confidence {opp.confidence} · risk {opp.risk_score} ({opp.safety_verdict})",
    ]
    if opp.opportunity_type == "income":
        details = []
        if opp.pay_type:
            rate = f" {_money(opp.pay_rate_usd)}{'/h' if opp.pay_type == 'hourly' else ''}" if opp.pay_rate_usd else ""
            details.append(f"{opp.get_pay_type_display()}{rate}")
        if opp.skill_match is not None:
            details.append(f"skill match {opp.skill_match}%")
        if opp.location_requirement:
            details.append(escape(opp.location_requirement[:60]))
        if details:
            lines.append(" · ".join(details))
        if opp.application_deadline:
            lines.append(f"Apply by: {timezone.localtime(opp.application_deadline):%b %d, %I:%M %p}")
    if opp.deadline:
        lines.append(f"Deadline: {timezone.localtime(opp.deadline):%b %d, %I:%M %p}")
    lines.append(_links(opp))
    return "\n".join(lines)


def send_new_opportunity_alerts(opportunities, now=None):
    """At most one alert per opportunity, ever (alert_sent_at)."""
    now = now or timezone.now()
    sent = []
    for opp in opportunities:
        if opp.alert_sent_at or opp.source.keep_in_app or not qualifies_for_alert(opp):
            continue  # Remotive / Remote OK jobs are never sent anywhere, Telegram included
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
            f"<b>{TYPE_LABELS[opp.opportunity_type]}: deadline in under 24h</b>",
            _title(opp, 150),
            f"Due {timezone.localtime(opp.deadline):%b %d, %I:%M %p} (about {hours}h left)",
            _links(opp),
        ]))
        sent.append(opp.pk)
    return sent


def _type_lines(opportunity_type, now, week_start):
    """One digest line (plus up to 3 best by EV/hour) for crypto or income."""
    new = Opportunity.objects.filter(opportunity_type=opportunity_type, discovered_at__gte=now - timedelta(hours=24))
    actionable = new.exclude(safety_verdict="block").filter(excluded=False)
    blocked = new.filter(safety_verdict="block").count()
    earned = Earning.objects.filter(kind="confirmed", opportunity_type=opportunity_type, date__gte=week_start).aggregate(
        total=Sum("usd_value_at_receipt"))["total"] or Decimal("0")
    lines = [f"{TYPE_LABELS[opportunity_type]}: {actionable.count()} new (24h), {blocked} blocked · "
             f"earned this week {_money(earned)}"]
    best = actionable.exclude(ev_per_hour__isnull=True).exclude(source__kind__in=list(Source.ATTRIBUTED_KINDS))
    if opportunity_type == "income":  # freelance below the skill-match cutoff is hidden by default - here too
        best = best.exclude(reward_type="freelance", skill_match__lt=int(config.get("freelance_min_skill_match")))
    for opp in best.order_by("-ev_per_hour")[:3]:
        lines.append(f"  • {escape(opp.title[:70])} - {_money(opp.ev_per_hour)}/h ({opp.confidence})")
    return lines


def digest_lines(now=None):
    """The daily digest's "Opportunities" section: a Crypto and an Income
    line (new opportunities in 24h, blocked, confirmed earnings this week,
    best by EV/hour), then deadlines coming up (7 days, things I'm doing or
    have submitted) and total confirmed earnings this week."""
    now = now or timezone.now()
    week_start = timezone.localdate(now) - timedelta(days=6)
    deadlines = Opportunity.objects.filter(status__in=("doing", "submitted"), deadline__gt=now,
                                           deadline__lte=now + timedelta(days=7)).order_by("deadline")
    earned = Earning.objects.filter(kind="confirmed", date__gte=week_start).aggregate(
        total=Sum("usd_value_at_receipt"))["total"] or Decimal("0")

    lines = ["<b>Opportunities</b>"]
    for opportunity_type, _label in OPPORTUNITY_TYPES:
        lines += _type_lines(opportunity_type, now, week_start)
    if deadlines:
        lines.append("Deadlines (7 days):")
        for opp in deadlines[:5]:
            lines.append(f"  • {timezone.localtime(opp.deadline):%b %d} - {_title(opp, 70)} "
                         f"({TYPE_LABELS[opp.opportunity_type].lower()}, {opp.status})")
    else:
        lines.append("Deadlines (7 days): none")
    lines.append(f"Earned this week (confirmed): {_money(earned)}")
    lines.append("")
    return lines
