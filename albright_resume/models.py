"""Models for the public resume page and its chatbot.

Everything the public page or the chatbot can show lives in these tables and
nowhere else. No visitor IPs or personal data are stored: rate-limit counters
hold only a keyed hash of the IP that expires within a day, and question logs
hold the (redacted) question text and the outcome."""
from decimal import Decimal

from django.conf import settings
from django.db import models

PLACEHOLDER = "[PLACEHOLDER"

DEFAULT_OFF_LIMITS = "\n".join([
    "Salary, compensation, or rate expectations",
    "Home address or exact location",
    "References' names or contact details",
    "Age, health, family, religion, politics, or other personal matters",
    "Personal finances, trading account balances, or investment positions",
])

DEFAULT_STARTERS = "\n".join([
    "What has Nathan built recently?",
    "What are his strongest technical skills?",
    "How does the options trading engine work?",
    "What kind of role is he looking for?",
])


class ResumeSettings(models.Model):
    """Singleton (pk=1): page/chat switches, profile header, chatbot config."""
    page_enabled = models.BooleanField(default=False, help_text="Public page at /resume/ is live.")
    chat_enabled = models.BooleanField(default=False, help_text="Chatbot is live for visitors.")

    full_name = models.CharField(max_length=120, default="Nathan Albright")
    headline = models.CharField(max_length=200, blank=True)
    location = models.CharField(max_length=100, blank=True, help_text="City/region only, never a street address.")
    open_to = models.CharField(max_length=200, blank=True, help_text="e.g. 'Open to full-time software roles'.")
    summary = models.TextField(blank=True)
    linkedin_url = models.URLField(blank=True)
    github_url = models.URLField(blank=True)
    contact_email = models.EmailField(blank=True, help_text="Shown as the 'Contact me' button (mailto).")
    book_call_url = models.URLField(blank=True, help_text="Calendly / Cal.com / Google booking link.")

    resume_pdf = models.BinaryField(null=True, blank=True, editable=False)
    resume_pdf_name = models.CharField(max_length=100, blank=True)
    resume_pdf_updated_at = models.DateTimeField(null=True, blank=True)

    knowledge_doc = models.TextField(blank=True, help_text="The chatbot answers only from this (plus published resume content if enabled). Treat it as public.")
    include_resume_in_chat = models.BooleanField(default=True, help_text="Also let the chatbot read the published resume sections on the page.")
    off_limits_topics = models.TextField(blank=True, default=DEFAULT_OFF_LIMITS, help_text="One per line. Declined politely.")
    starter_questions = models.TextField(blank=True, default=DEFAULT_STARTERS, help_text="One per line; first 4 are shown.")

    monthly_cap_usd = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("5.00"))
    max_message_chars = models.PositiveIntegerField(default=500)
    max_messages_per_session = models.PositiveIntegerField(default=12)
    per_ip_per_minute = models.PositiveIntegerField(default=4)
    per_ip_per_hour = models.PositiveIntegerField(default=20)
    per_ip_per_day = models.PositiveIntegerField(default=40)
    global_per_day = models.PositiveIntegerField(default=400, help_text="All visitors combined, per day.")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "resume settings"
        verbose_name_plural = "resume settings"

    def __str__(self):
        return "Resume settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @staticmethod
    def _lines(text):
        return [line.strip() for line in (text or "").splitlines() if line.strip()]

    @property
    def off_limits_list(self):
        return self._lines(self.off_limits_topics)

    @property
    def starter_list(self):
        return self._lines(self.starter_questions)[:4]

    @property
    def has_pdf(self):
        return bool(self.resume_pdf)


class OrderedPublished(models.Model):
    order = models.PositiveIntegerField(default=0, help_text="Lower shows first.")
    published = models.BooleanField(default=False)

    class Meta:
        abstract = True
        ordering = ["order", "id"]


class WorkExperience(OrderedPublished):
    role = models.CharField(max_length=150)
    organization = models.CharField(max_length=150)
    location = models.CharField(max_length=100, blank=True)
    start_label = models.CharField(max_length=40, blank=True, help_text="e.g. 'Jan 2023'")
    end_label = models.CharField(max_length=40, blank=True, help_text="e.g. 'Present'")
    highlights = models.TextField(blank=True, help_text="One bullet per line.")

    class Meta(OrderedPublished.Meta):
        pass

    def __str__(self):
        return f"{self.role} — {self.organization}"

    @property
    def highlight_list(self):
        return ResumeSettings._lines(self.highlights)


class EducationItem(OrderedPublished):
    KIND_CHOICES = [("education", "Education"), ("certification", "Certification")]
    kind = models.CharField(max_length=20, choices=KIND_CHOICES, default="education")
    title = models.CharField(max_length=150, help_text="Degree or certificate name.")
    institution = models.CharField(max_length=150, blank=True)
    year_label = models.CharField(max_length=40, blank=True)
    details = models.TextField(blank=True)

    class Meta(OrderedPublished.Meta):
        pass

    def __str__(self):
        return self.title


class Skill(OrderedPublished):
    name = models.CharField(max_length=80)
    category = models.CharField(max_length=60, blank=True, help_text="e.g. 'Languages', 'Frameworks'.")

    class Meta:
        ordering = ["category", "order", "name"]

    def __str__(self):
        return self.name


class Project(OrderedPublished):
    IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")
    title = models.CharField(max_length=120)
    tagline = models.CharField(max_length=200, blank=True)
    description = models.TextField(blank=True, help_text="Shown on the public page.")
    details_for_ai = models.TextField(blank=True, help_text="Longer notes: what it does, how you built it, results. Read by the chatbot (when published) and the AI summarizer. Treat as public.")
    tech = models.CharField(max_length=300, blank=True, help_text="Comma-separated, e.g. 'Python, Django, Postgres'.")
    link_url = models.URLField(blank=True)
    repo_url = models.URLField(blank=True)
    show_in_built = models.BooleanField(default=True, help_text="Feature in the 'What I've built' section.")
    screenshot = models.BinaryField(null=True, blank=True, editable=False)
    screenshot_content_type = models.CharField(max_length=20, blank=True)
    screenshot_alt = models.CharField(max_length=200, blank=True)
    ai_summary = models.TextField(blank=True, help_text="Last AI summary you applied (reference only).")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta(OrderedPublished.Meta):
        pass

    def __str__(self):
        return self.title

    @property
    def tech_list(self):
        return [t.strip() for t in (self.tech or "").split(",") if t.strip()]

    @property
    def has_screenshot(self):
        return bool(self.screenshot)


class AIDraft(models.Model):
    """AI-generated summary / skills / project blurbs. Never published until
    Nathan reviews it and presses Apply."""
    STATUS_CHOICES = [("draft", "Draft"), ("applied", "Applied"), ("discarded", "Discarded")]
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    summary = models.TextField(blank=True)
    skills = models.TextField(blank=True, help_text="One per line: 'Category: Skill'.")
    project_summaries = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="draft")
    applied_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"AI draft #{self.pk}"


class AICall(models.Model):
    """One provider call: tokens and cost only (never message content). The
    monthly spend cap is the sum of cost_usd for the current month."""
    PURPOSES = [("chat", "Chat"), ("summarize", "Summarize")]
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    purpose = models.CharField(max_length=12, choices=PURPOSES)
    model = models.CharField(max_length=60)
    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_write_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cost_usd = models.DecimalField(max_digits=10, decimal_places=6, default=Decimal("0"))
    ok = models.BooleanField(default=True)
    error = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created_at"]


class QuestionLog(models.Model):
    """Anonymous: no IP, no session id, no user agent. Emails/phone numbers in
    the question are redacted before saving."""
    OUTCOMES = [
        ("answered", "Answered"), ("not_sure", "Not in profile"), ("declined", "Declined (off-limits)"),
        ("rate_limited", "Rate limited"), ("cap", "Spend cap reached"), ("unavailable", "Unavailable"),
        ("error", "Error"),
    ]
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    question = models.TextField()
    reply = models.TextField(blank=True)
    outcome = models.CharField(max_length=14, choices=OUTCOMES, db_index=True)
    turn = models.PositiveSmallIntegerField(default=1)

    class Meta:
        ordering = ["-created_at"]


class RateCounter(models.Model):
    """Fixed-window counters in the DB (the site has no shared cache, and
    LocMemCache is per-process). key = scope:hmac(ip):window. Rows expire."""
    key = models.CharField(max_length=96, unique=True)
    count = models.PositiveIntegerField(default=0)
    expires_at = models.DateTimeField(db_index=True)
