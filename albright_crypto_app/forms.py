"""Forms. SECURITY RULE: no form here has (or may ever get) a field for a
seed phrase, private key, or exchange API key - tests/test_security.py
checks every form's field names. Free text is checked by reject_secrets()."""
from django import forms

from .extraction import quote_in_text
from .guards import reject_secrets
from .links import is_web_url
from . import my_profile
from .models import (APPLICATION_STATUSES, INCOME_REWARD_TYPES, OPPORTUNITY_TYPES, REWARD_TYPES_BY_OPPORTUNITY_TYPE,
                     Earning, MyProfile, Opportunity, Source, TimeLog)

INPUT = {"class": "ledger-input"}


def _lines(text):
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _skill_list(text):
    """Skills typed one per line or comma-separated -> a de-duplicated list."""
    skills = []
    for part in (text or "").replace("\n", ",").split(","):
        part = part.strip()[:60]
        if part and part.lower() not in (s.lower() for s in skills):
            skills.append(part)
    return skills


class ManualOpportunityForm(forms.Form):
    opportunity_type = forms.ChoiceField(choices=OPPORTUNITY_TYPES, initial="crypto", label="Type",
                                         widget=forms.Select(attrs=INPUT))
    url = forms.URLField(max_length=1000, widget=forms.URLInput(attrs={**INPUT, "placeholder": "https://..."}),
                         help_text="Where you found it.")
    title = forms.CharField(max_length=500, required=False, widget=forms.TextInput(attrs=INPUT))
    text = forms.CharField(widget=forms.Textarea(attrs={**INPUT, "rows": 10}),
                           help_text="Paste the description, terms, and reward details as they appear.")
    # Optional, income only - e.g. a paid task (annotation, AI training, testing platform) tracked by hand.
    reward_type = forms.ChoiceField(choices=[("", "Let the AI decide")] + [c for c in INCOME_REWARD_TYPES if c[0] != "unknown"],
                                    required=False, label="Income category (optional)", widget=forms.Select(attrs=INPUT))
    platform_name = forms.CharField(max_length=120, required=False, label="Platform (paid tasks)",
                                    widget=forms.TextInput(attrs={**INPUT, "placeholder": "e.g. an annotation platform"}))
    pay_rate_usd = forms.DecimalField(max_digits=10, decimal_places=2, min_value=0, required=False,
                                      label="Pay rate (USD per hour)", widget=forms.NumberInput(attrs={**INPUT, "step": "0.01"}))
    application_status = forms.ChoiceField(choices=[("", "-")] + APPLICATION_STATUSES, required=False,
                                           label="Application status", widget=forms.Select(attrs=INPUT))

    def clean_platform_name(self):
        name = self.cleaned_data["platform_name"].strip()
        reject_secrets(name)
        return name

    def clean_url(self):
        url = self.cleaned_data["url"]
        if not is_web_url(url):
            raise forms.ValidationError("Enter an http(s) link.")
        return url

    def clean_text(self):
        text = self.cleaned_data["text"]
        reject_secrets(text)
        return text


INCOME_FIELDS = ["pay_type", "pay_rate_usd", "skills", "location_requirement", "application_deadline",
                 "hours_is_estimate", "refundable_deposit_usd", "platform_name", "application_status"]
DATETIME_LOCAL = {**INPUT, "type": "datetime-local"}


class OpportunityEditForm(forms.ModelForm):
    """Correcting the extracted facts - saving re-runs the safety check and
    scoring (and skill_match). Reward types are the opportunity's type's;
    the income fields only appear for income."""
    skills = forms.CharField(required=False, label="Skills required", widget=forms.TextInput(attrs=INPUT),
                             help_text="Comma-separated.")

    class Meta:
        model = Opportunity
        fields = ["reward_usd", "reward_currency", "reward_type", "evidence", "evidence_quote", "deadline",
                  "hours_estimate", "costs_usd", "requires_spending",
                  "pay_type", "pay_rate_usd", "location_requirement", "application_deadline",
                  "hours_is_estimate", "refundable_deposit_usd", "platform_name", "application_status",
                  "eligibility_notes", "notes"]
        widgets = {
            "deadline": forms.DateTimeInput(attrs=DATETIME_LOCAL, format="%Y-%m-%dT%H:%M"),
            "application_deadline": forms.DateTimeInput(attrs=DATETIME_LOCAL, format="%Y-%m-%dT%H:%M"),
            "evidence_quote": forms.TextInput(attrs=INPUT),
            "eligibility_notes": forms.Textarea(attrs={**INPUT, "rows": 2}),
            "notes": forms.Textarea(attrs={**INPUT, "rows": 3}),
        }
        labels = {"pay_rate_usd": "Pay rate (USD; per hour for hourly)", "hours_is_estimate": "Hours are an estimate",
                  "refundable_deposit_usd": "Refundable deposit (USD - not spending)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        opportunity_type = self.instance.opportunity_type
        self.fields["reward_type"].choices = REWARD_TYPES_BY_OPPORTUNITY_TYPE[opportunity_type]
        if opportunity_type == "income":
            self.fields["skills"].initial = ", ".join(self.instance.skills_required or [])
            # Group the income fields after the shared ones, before the notes.
            order = [f for f in self.fields if f not in INCOME_FIELDS + ["eligibility_notes", "notes"]]
            self.order_fields(order + INCOME_FIELDS + ["eligibility_notes", "notes"])
        else:
            for name in INCOME_FIELDS:
                self.fields.pop(name)
        for name, field in self.fields.items():
            if not isinstance(field.widget, forms.CheckboxInput):
                field.widget.attrs.setdefault("class", "ledger-input")

    def clean_skills(self):
        return _skill_list(self.cleaned_data.get("skills"))

    def save(self, commit=True):
        if "skills" in self.fields:
            self.instance.skills_required = self.cleaned_data.get("skills") or []
        return super().save(commit=commit)

    def clean(self):
        cleaned = super().clean()
        quote = (cleaned.get("evidence_quote") or "").strip()
        source_text = f"{self.instance.title}\n{self.instance.raw_text}"
        if quote and not quote_in_text(quote, source_text):
            self.add_error("evidence_quote", "This quote doesn't appear in the opportunity's text.")
        if cleaned.get("evidence") in ("explicit", "stated") and not quote:
            self.add_error("evidence_quote", "Evidence needs a quote from the text that states the reward.")
        return cleaned


class TimeLogForm(forms.ModelForm):
    class Meta:
        model = TimeLog
        fields = ["minutes", "date", "note"]
        widgets = {"minutes": forms.NumberInput(attrs={**INPUT, "min": 1}),
                   "date": forms.DateInput(attrs={**INPUT, "type": "date"}),
                   "note": forms.TextInput(attrs=INPUT)}


class EarningForm(forms.ModelForm):
    """Confirmed earnings only. tx_link must be on an allowlisted domain
    (Earning.clean)."""
    class Meta:
        model = Earning
        fields = ["date", "amount", "currency", "usd_value_at_receipt", "tx_link", "note"]
        widgets = {"date": forms.DateInput(attrs={**INPUT, "type": "date"}),
                   "amount": forms.NumberInput(attrs={**INPUT, "step": "any"}),
                   "currency": forms.TextInput(attrs=INPUT),
                   "usd_value_at_receipt": forms.NumberInput(attrs={**INPUT, "step": "0.01"}),
                   "tx_link": forms.URLInput(attrs={**INPUT, "placeholder": "Block explorer link (verified domains only)"}),
                   "note": forms.TextInput(attrs=INPUT)}
        labels = {"usd_value_at_receipt": "USD value when received", "tx_link": "Transaction link"}


class SettingsForm(forms.Form):
    skills = forms.CharField(
        required=False, widget=forms.Textarea(attrs={**INPUT, "rows": 5}),
        help_text="One per line (or comma-separated). An income opportunity's skill match is the share of its "
                  "required skills in this list.")
    allowlist = forms.CharField(
        required=False, widget=forms.Textarea(attrs={**INPUT, "rows": 6}),
        help_text="One domain per line (e.g. github.com). Subdomains are included. Links anywhere else show a warning first. "
                  "remotive.com and remoteok.com are always included (their job-board adapters).")
    rss_feeds = forms.CharField(
        required=False, widget=forms.Textarea(attrs={**INPUT, "rows": 4}),
        help_text="One feed URL per line - official project and program feeds only.")

    def clean_allowlist(self):
        domains = []
        for line in _lines(self.cleaned_data["allowlist"]):
            domain = line.lower().removeprefix("https://").removeprefix("http://").split("/")[0].removeprefix("www.")
            if "." not in domain or " " in domain:
                raise forms.ValidationError(f"“{line}” isn't a domain.")
            if domain not in domains:
                domains.append(domain)
        return domains

    def clean_rss_feeds(self):
        feeds = _lines(self.cleaned_data["rss_feeds"])
        for feed in feeds:
            if not is_web_url(feed):
                raise forms.ValidationError(f"“{feed}” isn't an http(s) link.")
        return list(dict.fromkeys(feeds))

    def clean_skills(self):
        return _skill_list(self.cleaned_data["skills"])


class IncomeSourceForm(forms.ModelForm):
    """Adding an income source: Remotive or Remote OK (fixed public APIs -
    optional category/search or tag), or any official remote-job/freelance
    feed or public JSON API by URL. It starts with terms unchecked, so it
    won't run until I tick "terms checked" in the sources table. Only public
    feeds/APIs: no page scraping and no logging in (collectors only GET)."""
    kind = forms.ChoiceField(choices=[(k, label) for k, label in Source.KINDS if k in Source.INCOME_KINDS],
                             widget=forms.Select(attrs=INPUT))
    category = forms.CharField(required=False, max_length=100, widget=forms.TextInput(attrs={
        **INPUT, "placeholder": "e.g. software-dev"}), label="Remotive category (optional)")
    search = forms.CharField(required=False, max_length=100, widget=forms.TextInput(attrs={
        **INPUT, "placeholder": "e.g. django"}), label="Remotive search (optional)")
    tag = forms.CharField(required=False, max_length=60, widget=forms.TextInput(attrs={
        **INPUT, "placeholder": "e.g. python"}), label="Tag - Remote OK / Jobicy (optional)")

    class Meta:
        model = Source
        fields = ["name", "kind", "url", "notes"]
        widgets = {"name": forms.TextInput(attrs=INPUT),
                   "url": forms.URLInput(attrs={**INPUT, "placeholder": "https://... (official feed or public API)"}),
                   "notes": forms.TextInput(attrs={**INPUT, "placeholder": "Where the terms are, what they allow"})}
        labels = {"url": "Feed / API URL (not needed for Remotive / Remote OK)"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["url"].required = False   # required below, except for the job-board adapters
        self.fields["name"].required = False
        self.instance.opportunity_type = "income"
        self.instance.terms_checked = False  # ticked in the sources table once I've read the terms

    def clean_url(self):
        url = (self.cleaned_data.get("url") or "").strip()
        if url and not is_web_url(url):
            raise forms.ValidationError("Enter an http(s) link.")
        return url

    def clean_notes(self):
        notes = self.cleaned_data["notes"]
        reject_secrets(notes)
        return notes

    def clean(self):
        from .collectors.job_boards import ENDPOINTS, endpoint_url
        cleaned = super().clean()
        kind = cleaned.get("kind")
        if kind == "hn_hiring":
            # The official HN API, always - the latest monthly thread is found automatically.
            from .collectors.hn_hiring import API as HN_API
            self.instance.options = {}
            cleaned["url"] = HN_API
            cleaned["name"] = cleaned.get("name") or "HN: Who is hiring"
        elif kind in Source.ATTRIBUTED_KINDS:
            # The request always goes to the site's fixed endpoint; url is that
            # endpoint plus the options, so each combination is its own source.
            options = {k: (cleaned.get(k) or "").strip() for k in ENDPOINTS[kind][1] if (cleaned.get(k) or "").strip()}
            self.instance.options = options
            cleaned["url"] = endpoint_url(kind, options)
            if not cleaned.get("name"):
                site = Source.ATTRIBUTED_KINDS[kind][0]
                cleaned["name"] = f"{site}: {', '.join(options.values())}" if options else site
        else:
            self.instance.options = {}
            if not cleaned.get("url") and "url" not in self.errors:
                self.add_error("url", "Enter the feed or API URL.")
            if not cleaned.get("name"):
                self.add_error("name", "Give the source a name.")
        if Source.objects.filter(kind=kind, url=cleaned.get("url"), opportunity_type="income").exists():
            raise forms.ValidationError("That source is already in your list.")
        return cleaned


class GmailLabelForm(forms.Form):
    label = forms.CharField(max_length=100, initial="opportunities", widget=forms.TextInput(attrs=INPUT),
                            help_text="The Gmail label your job-alert emails go to (set up a Gmail filter to apply it).")


def _textarea(rows, placeholder=""):
    return forms.Textarea(attrs={**INPUT, "rows": rows, "placeholder": placeholder})


class ProfileForm(forms.Form):
    """My profile - the only facts the resume and proposal generators may
    use. Plain-text formats (parsed by my_profile.parse_*)."""
    name = forms.CharField(max_length=120, required=False, widget=forms.TextInput(attrs=INPUT))
    headline = forms.CharField(max_length=200, required=False, widget=forms.TextInput(attrs=INPUT))
    summary = forms.CharField(required=False, widget=_textarea(4))
    skills = forms.CharField(required=False, widget=_textarea(4, "Python, Django, PostgreSQL ..."),
                             help_text="One per line or comma-separated.")
    work_history = forms.CharField(required=False, widget=_textarea(10, "Employer | Title | 2019-01 | 2023-06\n- What you did"),
                                   help_text="Employer | Title | Start | End, then \"- bullet\" lines. Blank line between jobs.")
    projects = forms.CharField(required=False, widget=_textarea(6, "Project | https://link\n- What it does"),
                               help_text="Name | link (optional), then \"- bullet\" lines.")
    education = forms.CharField(required=False, widget=_textarea(3), help_text="School | Degree | Year, one per line.")
    certifications = forms.CharField(required=False, widget=_textarea(3), help_text="One per line.")
    links = forms.CharField(required=False, widget=_textarea(3, "GitHub | https://github.com/..."),
                            help_text="Label | URL, one per line.")
    resume_template = forms.CharField(required=False, widget=_textarea(12),
                                      help_text="Placeholders: " + ", ".join("{%s}" % p for p in my_profile.RESUME_PLACEHOLDERS))
    proposal_template = forms.CharField(required=False, widget=_textarea(8),
                                        help_text="Placeholders: " + ", ".join("{%s}" % p for p in my_profile.PROPOSAL_PLACEHOLDERS))

    @classmethod
    def initial_for(cls, profile):
        return {
            "name": profile.name, "headline": profile.headline, "summary": profile.summary,
            "skills": "\n".join(profile.skills or []),
            "work_history": my_profile.format_work_history(profile.work_history),
            "projects": my_profile.format_projects(profile.projects),
            "education": my_profile.format_education(profile.education),
            "certifications": "\n".join(profile.certifications or []),
            "links": my_profile.format_links(profile.links),
            "resume_template": profile.resume_template or my_profile.DEFAULT_RESUME_TEMPLATE,
            "proposal_template": profile.proposal_template or my_profile.DEFAULT_PROPOSAL_TEMPLATE,
        }

    def clean(self):
        cleaned = super().clean()
        for name, value in list(cleaned.items()):
            if isinstance(value, str):
                reject_secrets(value)
        links = my_profile.parse_links(cleaned.get("links"))
        for link in links:
            if not is_web_url(link.get("url")):
                self.add_error("links", f"“{link.get('url')}” isn't an http(s) link.")
        for job in my_profile.parse_work_history(cleaned.get("work_history")):
            if not job["employer"] or not job["title"]:
                self.add_error("work_history", f"“{job['employer'] or job['title']}”: use Employer | Title | Start | End.")
        return cleaned

    def save(self, profile):
        d = self.cleaned_data
        profile.name, profile.headline, profile.summary = d["name"], d["headline"], d["summary"]
        profile.skills = my_profile.parse_skills(d["skills"])
        profile.work_history = my_profile.parse_work_history(d["work_history"])
        profile.projects = my_profile.parse_projects(d["projects"])
        profile.education = my_profile.parse_education(d["education"])
        profile.certifications = my_profile.parse_lines(d["certifications"])
        profile.links = my_profile.parse_links(d["links"])
        profile.resume_template, profile.proposal_template = d["resume_template"], d["proposal_template"]
        profile.save()
        return profile
