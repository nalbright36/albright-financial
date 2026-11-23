"""Roundups: a short briefing of what's coming (Home's card and /briefing/).

  Roundup       one schedule of one person's: what it covers (scope), when it's made (freq, weekdays / month_day,
                send_time in their Planner zone), which sections, where it pings (push / email), and whether their
                own AI writes "The short version". Several per person (three presets today: Week ahead, Today,
                Month ahead; the model allows more). next_run_at (UTC) is when send_roundups makes the next one.
  RoundupIssue  one made briefing: the snapshot (payload) of what it said, so history shows what you were told.
                Built only from what the owner could see at the time (albright_roundup.build). Owner-only.
"""
from django.conf import settings
from django.db import models
from django.utils import timezone

TODAY, WEEK, MONTH = "today", "week", "month"
SCOPES = [(TODAY, "Today"), (WEEK, "Week"), (MONTH, "Month")]
DAILY, WEEKDAYS, WEEKLY, MONTHLY = "daily", "weekdays", "weekly", "monthly"
FREQS = [(DAILY, "Every day"), (WEEKDAYS, "Weekdays"), (WEEKLY, "Weekly"), (MONTHLY, "Monthly")]
SECTIONS = ["attention", "days", "repeats", "weather", "meals", "fitness", "money", "shared"]
PUSH, EMAIL = "push", "email"
RULES, AI = "rules", "ai"


def default_sections():
    return list(SECTIONS)


def default_channels():
    return [PUSH]


class Roundup(models.Model):
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="roundups")
    name = models.CharField(max_length=60)
    scope = models.CharField(max_length=10, choices=SCOPES, default=WEEK)
    freq = models.CharField(max_length=10, choices=FREQS, default=WEEKLY)
    weekdays = models.JSONField(default=list, blank=True)        # Mon=0 .. Sun=6 (weekly)
    month_day = models.PositiveSmallIntegerField(default=1)      # 1-28 (monthly)
    send_time = models.TimeField()
    sections = models.JSONField(default=default_sections, blank=True)
    channels = models.JSONField(default=default_channels, blank=True)
    ai_summary = models.BooleanField(default=False)
    skip_if_empty = models.BooleanField(default=False)
    enabled = models.BooleanField(default=True)
    position = models.PositiveSmallIntegerField(default=0)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["owner", "position", "pk"]

    def __str__(self):
        return f"{self.owner} · {self.name}"


class RoundupIssue(models.Model):
    roundup = models.ForeignKey(Roundup, on_delete=models.CASCADE, related_name="issues")
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="roundup_issues")
    scope = models.CharField(max_length=10, choices=SCOPES)
    period_start = models.DateField()
    period_end = models.DateField()
    payload = models.JSONField(default=dict)
    summary = models.TextField(blank=True)
    summary_source = models.CharField(max_length=5, default=RULES)
    built_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)
    delivered = models.JSONField(default=dict, blank=True)     # {"push": 2, "email": true}
    read_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-period_start", "-pk"]
        constraints = [models.UniqueConstraint(fields=["roundup", "period_start"], name="roundup_one_issue_a_period")]
        indexes = [models.Index(fields=["owner", "-built_at"], name="roundup_issue_owner_built")]

    def __str__(self):
        return f"{self.roundup} {self.period_start}"
