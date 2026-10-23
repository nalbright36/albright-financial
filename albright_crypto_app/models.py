"""Opportunities (app label albright_crypto_app - kept so migrations and
data stay put): crypto opportunities and general online income, side by side.

SECURITY RULE (enforced by tests/test_security.py): nothing here stores a
seed/recovery phrase, a private key, or an exchange API key with trading or
withdrawal rights. There is no wallet model at all; nothing in this app
signs or sends transactions. Free-text fields a person types into (notes,
pasted text) are checked by guards.reject_secrets() before saving.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import models
from django.utils import timezone

from .guards import reject_secrets

OPPORTUNITY_TYPES = [("crypto", "Crypto"), ("income", "Income")]
# Reward types each opportunity type may use (core.scoring knows all of them).
CRYPTO_REWARD_TYPES = [
    ("bounty", "Bounty"), ("promo_reward", "Promo reward"), ("microtask", "Microtask"), ("testnet", "Testnet"),
    ("speculative_airdrop", "Speculative airdrop"), ("points", "Points"), ("unknown", "Unknown"),
]
INCOME_REWARD_TYPES = [
    ("freelance", "Freelance"), ("bounty", "Bounty"), ("research_study", "Research study"),
    ("competition", "Competition"), ("bonus", "Bonus"), ("settlement", "Settlement"),
    ("paid_task", "Paid task"),   # annotation, AI training, testing platforms - tracked by hand
    ("unknown", "Unknown"),
]
APPLICATION_STATUSES = [
    ("not_applied", "Not applied"), ("applied", "Applied"), ("assessment", "Assessment"),
    ("approved", "Approved"), ("active", "Active"), ("rejected", "Rejected"),
]
REWARD_TYPES = CRYPTO_REWARD_TYPES[:-1] + [rt for rt in INCOME_REWARD_TYPES if rt[0] != "bounty"]
REWARD_TYPES_BY_OPPORTUNITY_TYPE = {"crypto": CRYPTO_REWARD_TYPES, "income": INCOME_REWARD_TYPES}
PAY_TYPES = [("hourly", "Hourly"), ("fixed", "Fixed price"), ("prize", "Prize"), ("bonus", "Bonus")]
EVIDENCE = [("explicit", "Explicit"), ("stated", "Stated"), ("none", "None")]
VERDICTS = [("ok", "OK"), ("warn", "Warning"), ("block", "Blocked")]
LEVELS = [("high", "High"), ("medium", "Medium"), ("low", "Low")]
AUTOMATION = [("high", "High"), ("medium", "Medium"), ("low", "Low"), ("none", "None")]
# The workflow: Inbox (triage, one at a time) -> Pipeline (Saved -> Applied ->
# Talking/Interview -> Working -> Paid, plus Closed) - or Skipped from the Inbox.
STATUSES = [
    ("inbox", "Inbox"), ("saved", "Saved"), ("applied", "Applied"), ("talking", "Talking / interview"),
    ("working", "Working"), ("paid", "Paid"), ("closed", "Closed"), ("skipped", "Skipped"),
]
PIPELINE_STAGES = ("saved", "applied", "talking", "working", "paid", "closed")
ACTIVE_STATUSES = ("inbox", "saved", "applied", "talking", "working")
CLOSE_REASONS = [("lost", "Lost / rejected"), ("declined", "Declined"), ("expired", "Expired")]
SKIP_REASONS = [("not_relevant", "Not relevant"), ("pay", "Pay too low"), ("location", "Location"),
                ("seniority", "Seniority")]
# What kind of work it is - the Inbox's Type filter and Settings' work types.
WORK_TYPES = [
    ("full_time", "Full-time"), ("part_time", "Part-time"), ("contract", "Contract"), ("freelance", "Freelance"),
    ("bounty", "Bounty"), ("paid_task", "Paid task"), ("crypto", "Crypto"), ("other", "Other"),
]
FIT_METHODS = [("ai", "AI"), ("keywords", "Keywords")]
CENT = Decimal("0.01")
EXTRACTION_STATES = [("pending", "Pending"), ("done", "Done"), ("skipped", "Skipped (limit)"), ("error", "Error"),
                     ("filtered", "Skipped (filtered out)")]


class ConfigOverride(models.Model):
    """A CRYPTO setting edited from the app's Settings page (allowlist,
    rss_feeds) - read before settings.py's CRYPTO dict (config.get())."""
    key = models.CharField(max_length=60, unique=True)
    value = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.key} = {self.value}"


class Source(models.Model):
    KINDS = [("github", "GitHub"), ("rss", "RSS"), ("manual", "Manual"),
             ("rss_income", "RSS (income)"), ("api_income", "API (income)"),
             ("remotive", "Remotive"), ("remoteok", "Remote OK"),
             ("himalayas", "Himalayas"), ("jobicy", "Jobicy"), ("arbeitnow", "Arbeitnow"),
             ("hn_hiring", "HN: Who is hiring"), ("email", "Email alerts")]
    # Never run until terms_checked is ticked.
    INCOME_KINDS = ("rss_income", "api_income", "remotive", "remoteok", "himalayas", "jobicy", "arbeitnow",
                    "hn_hiring")
    # Job boards whose terms require crediting them ("via ...", linking to the
    # job on their site) and forbid passing their jobs on: (name, domain).
    # Their opportunities stay in this app - no AI extraction, no Telegram.
    ATTRIBUTED_KINDS = {"remotive": ("Remotive", "remotive.com"), "remoteok": ("Remote OK", "remoteok.com"),
                        "himalayas": ("Himalayas", "himalayas.app"), "jobicy": ("Jobicy", "jobicy.com"),
                        "arbeitnow": ("Arbeitnow", "arbeitnow.com")}

    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=10, choices=KINDS)
    opportunity_type = models.CharField(max_length=6, choices=OPPORTUNITY_TYPES, default="crypto")
    url = models.CharField(max_length=500, blank=True)   # feed URL, or the GitHub search query
    active = models.BooleanField(default=True)
    last_checked = models.DateTimeField(null=True, blank=True)
    terms_checked = models.BooleanField(default=False)     # I've read this source's terms - collecting is allowed
    notes = models.TextField(blank=True)
    # Request parameters for the job-board adapters (Remotive: category,
    # search; Remote OK: tag). Also reflected in url so each combination is unique.
    options = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["opportunity_type", "kind", "name"]
        unique_together = ("kind", "url", "opportunity_type")

    def __str__(self):
        return f"{self.get_kind_display()}: {self.name}"

    def clean(self):
        reject_secrets(self.notes, "notes")

    @property
    def needs_terms(self):
        return self.kind in self.INCOME_KINDS

    @property
    def runnable(self):
        """Active, and - for income sources - its terms have been checked.
        Email alerts need a connected Gmail account instead (my own mail)."""
        if self.kind == "email":
            return self.active and GmailConnection.objects.filter(pk=GmailConnection.SINGLETON_PK).exists()
        return self.active and (not self.needs_terms or self.terms_checked)

    @property
    def attribution(self):
        """(name, domain) for Remotive / Remote OK, else None."""
        return self.ATTRIBUTED_KINDS.get(self.kind)

    @property
    def keep_in_app(self):
        """This source's jobs are never sent anywhere (AI provider, Telegram)."""
        return self.kind in self.ATTRIBUTED_KINDS


class Opportunity(models.Model):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name="opportunities")
    opportunity_type = models.CharField(max_length=6, choices=OPPORTUNITY_TYPES, default="crypto")
    external_id = models.CharField(max_length=300)
    url = models.URLField(max_length=1000)
    title = models.CharField(max_length=500)
    raw_text = models.TextField(blank=True)
    discovered_at = models.DateTimeField(default=timezone.now)
    metadata = models.JSONField(default=dict, blank=True)   # GitHub: labels, repo, created/updated dates

    # Facts (AI extraction - only what the text states; null/"unknown" otherwise)
    deadline = models.DateTimeField(null=True, blank=True)
    reward_usd = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    reward_currency = models.CharField(max_length=20, blank=True)
    reward_type = models.CharField(max_length=24, choices=REWARD_TYPES, default="unknown")
    evidence = models.CharField(max_length=10, choices=EVIDENCE, default="none")
    evidence_quote = models.CharField(max_length=500, blank=True)
    official_source = models.BooleanField(default=False)
    hours_estimate = models.DecimalField(max_digits=7, decimal_places=2, null=True, blank=True)
    costs_usd = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    requires_spending = models.BooleanField(default=False)
    steps = models.JSONField(default=list, blank=True)      # [{"text": str, "automatable": bool}]
    automatable_steps = models.PositiveSmallIntegerField(default=0)
    eligibility_notes = models.TextField(blank=True)
    hours_is_estimate = models.BooleanField(default=False)   # True: hours are an estimate, not stated
    # A deposit you get back (e.g. a bank bonus's opening deposit) - never counted as spending.
    refundable_deposit_usd = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)

    # Income only (null for crypto)
    pay_type = models.CharField(max_length=6, choices=PAY_TYPES, null=True, blank=True)
    pay_rate_usd = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    skills_required = models.JSONField(null=True, blank=True)
    skill_match = models.PositiveSmallIntegerField(null=True, blank=True)   # 0-100: share of skills_required I have
    location_requirement = models.CharField(max_length=200, null=True, blank=True)
    application_deadline = models.DateTimeField(null=True, blank=True)
    # Paid tasks (annotation, AI training, testing platforms) - tracked by hand.
    platform_name = models.CharField(max_length=120, blank=True)
    application_status = models.CharField(max_length=12, choices=APPLICATION_STATUSES, blank=True)
    extraction_status = models.CharField(max_length=10, choices=EXTRACTION_STATES, default="pending")

    # Safety (core.safety.check_opportunity)
    safety_verdict = models.CharField(max_length=6, choices=VERDICTS, default="warn")
    risk_score = models.PositiveSmallIntegerField(default=0)
    safety_reasons = models.JSONField(default=list, blank=True)
    domain = models.CharField(max_length=255, blank=True)
    domain_status = models.CharField(max_length=12, blank=True)

    # Scores (core.scoring.score) - four separate scores, never merged
    excluded = models.BooleanField(default=False)
    exclude_reason = models.CharField(max_length=200, blank=True)
    ev_low = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ev_mid = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ev_high = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    ev_per_hour = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    confidence = models.CharField(max_length=6, choices=LEVELS, default="low")
    automation = models.CharField(max_length=6, choices=AUTOMATION, default="none")
    reward_counts_as_confirmed = models.BooleanField(default=False)

    # Relevance (relevance.py): what kind of work, who's hiring, when it was posted,
    # and why it's hidden from the Inbox ("" = not hidden). A free prefilter
    # (Settings > Filters) runs first; what passes gets a fit score (0-100) -
    # from the AI, or from keywords for job boards whose terms keep their jobs
    # out of the AI. Bounties, paid tasks and crypto keep the EV/hour score.
    work_type = models.CharField(max_length=10, choices=WORK_TYPES, default="other")
    company = models.CharField(max_length=200, blank=True)
    posted_at = models.DateTimeField(null=True, blank=True)
    hidden_reason = models.CharField(max_length=200, blank=True)
    show_anyway = models.BooleanField(default=False)   # "Show in Inbox" from Hidden: filters no longer hide it
    fit_score = models.PositiveSmallIntegerField(null=True, blank=True)
    fit_method = models.CharField(max_length=8, choices=FIT_METHODS, blank=True)
    matched_skills = models.JSONField(default=list, blank=True)
    missing_requirements = models.JSONField(default=list, blank=True)
    role_match = models.CharField(max_length=120, blank=True)
    seniority = models.CharField(max_length=20, blank=True)
    fit_reason = models.CharField(max_length=300, blank=True)
    # The same posting found again elsewhere (same company, similar title,
    # within 30 days): merged into the first one, which keeps every link.
    duplicate_of = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="duplicates")
    other_links = models.JSONField(default=list, blank=True)   # [{"source": str, "url": str, "opportunity": pk}]

    status = models.CharField(max_length=10, choices=STATUSES, default="inbox")
    stage_changed_at = models.DateTimeField(null=True, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    followup_sent_at = models.DateTimeField(null=True, blank=True)
    close_reason = models.CharField(max_length=10, choices=CLOSE_REASONS, blank=True)
    skip_reason = models.CharField(max_length=12, choices=SKIP_REASONS, blank=True)
    notes = models.TextField(blank=True)
    alert_sent_at = models.DateTimeField(null=True, blank=True)
    deadline_reminder_sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("source", "external_id")
        ordering = ["-discovered_at"]
        verbose_name_plural = "opportunities"

    def __str__(self):
        return self.title[:80]

    def clean(self):
        reject_secrets(self.notes, "notes")

    @property
    def ev_per_hour_low(self):
        return (Decimal(self.ev_low) / Decimal(self.hours_estimate)).quantize(CENT) if self.hours_estimate else None

    @property
    def ev_per_hour_high(self):
        return (Decimal(self.ev_high) / Decimal(self.hours_estimate)).quantize(CENT) if self.hours_estimate else None

    @property
    def is_blocked(self):
        return self.safety_verdict == "block"

    @property
    def is_hidden(self):
        """Out of the Inbox and into Hidden: blocked, excluded (spending), or filtered out."""
        return self.safety_verdict == "block" or self.excluded or bool(self.hidden_reason)

    @property
    def hide_reasons(self):
        if self.safety_verdict == "block":
            return ["blocked: " + r for r in self.safety_reasons or []] or ["blocked by the safety check"]
        reasons = [f"excluded: {self.exclude_reason}"] if self.excluded else []
        return reasons + ([self.hidden_reason] if self.hidden_reason else [])

    @property
    def stage_since(self):
        return self.stage_changed_at or self.discovered_at

    @property
    def days_in_stage(self):
        return max(0, (timezone.now() - self.stage_since).days)

    @property
    def followup_due(self):
        """Applied 7+ days ago with no movement since."""
        from datetime import timedelta
        return (self.status == "applied" and self.applied_at is not None
                and self.stage_since <= timezone.now() - timedelta(days=7))

    @property
    def any_deadline(self):
        dates = [d for d in (self.application_deadline, self.deadline) if d]
        return min(dates) if dates else None


class TaskStep(models.Model):
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="task_steps")
    text = models.CharField(max_length=500)
    done = models.BooleanField(default=False)
    order = models.PositiveSmallIntegerField(default=0)
    automatable = models.BooleanField(default=False)

    class Meta:
        ordering = ["order", "pk"]

    def __str__(self):
        return self.text[:80]


class TimeLog(models.Model):
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="time_logs")
    minutes = models.PositiveIntegerField()
    date = models.DateField(default=timezone.localdate)
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-date", "-pk"]

    def clean(self):
        reject_secrets(self.note, "note")


class Earning(models.Model):
    KINDS = [("confirmed", "Confirmed")]

    opportunity = models.ForeignKey(Opportunity, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="earnings")
    opportunity_type = models.CharField(max_length=6, choices=OPPORTUNITY_TYPES, default="crypto")
    date = models.DateField(default=timezone.localdate)
    amount = models.DecimalField(max_digits=20, decimal_places=8)
    currency = models.CharField(max_length=20, default="USD")
    usd_value_at_receipt = models.DecimalField(max_digits=12, decimal_places=2)
    kind = models.CharField(max_length=10, choices=KINDS, default="confirmed")
    tx_link = models.URLField(max_length=500, blank=True)  # block-explorer link - allowlisted domains only
    note = models.CharField(max_length=300, blank=True)

    class Meta:
        ordering = ["-date", "-pk"]

    def clean(self):
        from . import config
        from .core.safety import check_domain
        reject_secrets(self.note, "note")
        if self.tx_link and check_domain(self.tx_link, config.allowlist())[1] != "allowlisted":
            raise ValidationError({"tx_link": "Transaction links must be on a domain in your verified list."})

    def save(self, *args, **kwargs):
        if self.opportunity_id:  # an earning's type is its opportunity's
            self.opportunity_type = self.opportunity.opportunity_type
        super().save(*args, **kwargs)


class AIExtraction(models.Model):
    """One AI call - counts toward ai_daily_limit and the shared monthly AI
    budget (with the reseller app's AI reviews). Most belong to an
    opportunity; splitting an email or filtering HN comments happens before
    there is one."""
    PURPOSES = [("extract", "Fact extraction"), ("email_split", "Email alert split"),
                ("hn_filter", "HN comment filter"), ("walkthrough", "Walkthrough"),
                ("resume", "Tailored resume"), ("proposal", "Proposal"), ("fit", "Fit score")]
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="extractions",
                                    null=True, blank=True)
    purpose = models.CharField(max_length=12, choices=PURPOSES, default="extract")
    created_at = models.DateTimeField(auto_now_add=True)
    model_name = models.CharField(max_length=60, blank=True)
    status = models.CharField(max_length=10, default="done")   # done / error
    cost_usd = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ["-created_at"]


class ScanRun(models.Model):
    """One scan_crypto run - what --min-interval-hours measures from."""
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    sources_checked = models.PositiveIntegerField(default=0)
    new_opportunities = models.PositiveIntegerField(default=0)
    errors = models.JSONField(default=list, blank=True)

    class Meta:
        ordering = ["-started_at"]


class ProcessedItem(models.Model):
    """An email message or HN comment a source has already handled, so it's
    never imported (or sent to the AI) twice - whether or not it produced
    any opportunities."""
    source = models.ForeignKey(Source, on_delete=models.CASCADE, related_name="processed_items")
    item_id = models.CharField(max_length=200)
    processed_at = models.DateTimeField(auto_now_add=True)
    opportunities_created = models.PositiveSmallIntegerField(default=0)

    class Meta:
        unique_together = ("source", "item_id")
        ordering = ["-processed_at"]

    def __str__(self):
        return f"{self.source_id}:{self.item_id}"


class GmailConnection(models.Model):
    """The one Gmail account the "Email alerts" source reads (scope
    gmail.readonly only). The OAuth token is stored encrypted with the same
    Fernet key as the Alpaca credentials - never in plaintext."""
    SINGLETON_PK = 1
    email = models.CharField(max_length=254, blank=True)
    encrypted_token = models.TextField()
    scope = models.CharField(max_length=200)
    connected_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def set_token(self, token):
        import json
        from albright_trading_app.crypto_utils import get_fernet
        self.encrypted_token = get_fernet().encrypt(json.dumps(token).encode()).decode()

    def get_token(self):
        import json
        from albright_trading_app.crypto_utils import get_fernet
        return json.loads(get_fernet().decrypt(self.encrypted_token.encode()).decode())

    def __str__(self):
        return f"Gmail: {self.email or 'connected'}"


class Walkthrough(models.Model):
    """A saved "Walk me through it" plan for an opportunity (regenerating
    makes a new one; older ones stay reopenable)."""
    KINDS = [("github", "GitHub bounty"), ("income", "Job / freelance / paid task"), ("crypto", "Crypto")]
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="walkthroughs")
    created_at = models.DateTimeField(auto_now_add=True)
    kind = models.CharField(max_length=8, choices=KINDS)
    content = models.JSONField(default=dict)
    cost = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    model_name = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"Walkthrough({self.opportunity_id}, {self.created_at:%Y-%m-%d %H:%M})"


class MyProfile(models.Model):
    """Structured facts about me - the ONLY source the resume and proposal
    generators may use - plus the editable resume and proposal templates
    (placeholders like {summary}, {experience}, {proposal_body})."""
    SINGLETON_PK = 1
    name = models.CharField(max_length=120, blank=True)
    headline = models.CharField(max_length=200, blank=True)
    summary = models.TextField(blank=True)
    # Target roles steer the fit score only - they're goals, not facts, so the
    # resume/proposal fact check never accepts them as experience.
    target_roles = models.JSONField(default=list, blank=True)
    years_experience = models.CharField(max_length=20, blank=True)   # e.g. "8+"
    industries = models.JSONField(default=list, blank=True)
    location = models.CharField(max_length=120, blank=True)
    skills = models.JSONField(default=list, blank=True)
    work_history = models.JSONField(default=list, blank=True)    # [{employer, title, start, end, bullets[]}]
    projects = models.JSONField(default=list, blank=True)        # [{name, link, bullets[]}]
    education = models.JSONField(default=list, blank=True)       # [{school, degree, year}]
    certifications = models.JSONField(default=list, blank=True)  # [str]
    links = models.JSONField(default=list, blank=True)           # [{label, url}]
    resume_template = models.TextField(blank=True)
    proposal_template = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"Profile: {self.name or '(unnamed)'}"

    def clean(self):
        for field in ("name", "headline", "summary", "resume_template", "proposal_template"):
            reject_secrets(getattr(self, field), field)

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=cls.SINGLETON_PK)[0]


class ApplicationDraft(models.Model):
    """A generated proposal or tailored resume for an income opportunity."""
    KINDS = [("proposal", "Proposal"), ("resume", "Tailored resume")]
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name="drafts")
    kind = models.CharField(max_length=8, choices=KINDS)
    created_at = models.DateTimeField(auto_now_add=True)
    text = models.TextField()                                  # what the copy button copies
    content = models.JSONField(default=dict, blank=True)       # resume structure (for .docx)
    key_requirements = models.JSONField(default=list, blank=True)
    unmet_requirements = models.JSONField(default=list, blank=True)
    removed_claims = models.JSONField(default=list, blank=True)   # sentences dropped as not in my profile
    cost = models.DecimalField(max_digits=8, decimal_places=4, default=0)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-pk"]

    def __str__(self):
        return f"{self.get_kind_display()} for {self.opportunity_id}"
