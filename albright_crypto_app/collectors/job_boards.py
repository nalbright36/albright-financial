"""Public remote-job APIs - Remotive, Remote OK, Himalayas, Jobicy and
Arbeitnow - one GET per fetch.

Their terms, enforced here and in pipeline.scan_sources (Himalayas, Jobicy
and Arbeitnow are treated exactly like the first two: attribution, a
followed link back, 6 hours between fetches, jobs kept in the app):
  - Remotive: at most 4 fetches a day and 2 requests a minute. Remote OK:
    a descriptive User-Agent. Both: at least MIN_INTERVAL (6 hours) between
    fetches - per site, so even several sources for one site (different
    categories/tags) can't add up to more - however often scan_crypto runs.
    No retries: a failed fetch waits for the next interval.
  - Credit them: every job shows "via Remotive" / "via Remote OK", linking
    to the job on their site (templatetags/crypto_links.via_link).
  - Don't pass their jobs on: Source.keep_in_app - no AI extraction, and
    no job details in Telegram alerts or the digest.

Only jobs whose URL is on the site's own domain are kept, so the
attribution link always points at their listing. The request always goes to
the fixed endpoint below (never to Source.url, which is only a label);
Source.options holds the optional parameters.
"""
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import urlencode

from .base import BaseCollector, RawOpportunity
from .utils import facts_text as _facts_text, on_site as _on_site, plain as _plain

MIN_INTERVAL = timedelta(hours=6)
REMOTEOK_USER_AGENT = ("AlbrightOpportunities/1.0 (personal remote-job search tool; reads remoteok.com/api at most "
                       "once every 6 hours; every job is shown with a link back to Remote OK)")


FREELANCE_JOB_TYPES = {"contract", "contractor", "freelance", "freelancer", "part_time", "parttime"}


def _reward_type(job_type):
    """Contract/freelance/part-time work is "freelance"; salaried jobs aren't
    one of the income reward types. Takes one job type or a list."""
    types = job_type if isinstance(job_type, (list, tuple)) else [job_type]
    normalized = {str(t or "").lower().replace("-", "_").replace(" ", "_") for t in types}
    return "freelance" if normalized & FREELANCE_JOB_TYPES else "unknown"


def _annual_salary(low, high, currency="USD"):
    def amount(v):
        try:
            return int(float(v)) if v not in (None, "") and float(v) > 0 else None
        except (TypeError, ValueError):
            return None
    low, high = amount(low), amount(high)
    symbol = "$" if (currency or "USD").upper() == "USD" else f"{currency} "
    if low and high:
        return f"{symbol}{low:,}–{symbol}{high:,} per year"
    if low or high:
        return f"{symbol}{(low or high):,} per year"
    return None


def _from_unix(value):
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _tags(value):
    return [str(t).strip()[:60] for t in value or [] if str(t).strip()][:30] if isinstance(value, list) else []


def endpoint_url(kind, options):
    """The URL a source of this kind fetches (also stored as Source.url, so each option set is unique)."""
    base, keys = ENDPOINTS[kind]
    params = {k: str(options[k]).strip() for k in keys if str(options.get(k) or "").strip()}
    return f"{base}?{urlencode(params)}" if params else base


class JobBoardCollector(BaseCollector):
    min_interval = MIN_INTERVAL
    retries = 0  # one request per fetch, never a second one
    endpoint = ""
    option_keys = ()
    domain = ""
    site_name = ""

    def params(self, source):
        options = source.options or {}
        return {k: str(options[k]).strip() for k in self.option_keys if str(options.get(k) or "").strip()} or None


class RemotiveCollector(JobBoardCollector):
    kind = "remotive"
    endpoint = "https://remotive.com/api/remote-jobs"
    option_keys = ("category", "search")
    domain = "remotive.com"
    site_name = "Remotive"

    def collect(self, source):
        payload = self.get(self.endpoint, params=self.params(source)).json()
        for job in (payload.get("jobs") if isinstance(payload, dict) else None) or []:
            if not isinstance(job, dict) or not _on_site(job.get("url"), self.domain) or not job.get("id"):
                continue
            location = str(job.get("candidate_required_location") or "").strip() or None
            facts = [("Company", job.get("company_name")), ("Category", job.get("category")),
                     ("Job type", job.get("job_type")), ("Location", location), ("Salary", job.get("salary"))]
            yield RawOpportunity(
                external_id=f"remotive:{job['id']}",
                url=job["url"],
                title=_plain(job.get("title") or job["url"])[:500],
                text=_facts_text(job.get("description"), facts),
                metadata={"via": self.site_name, "company": job.get("company_name") or "",
                          "category": job.get("category") or "", "job_type": job.get("job_type") or "",
                          "salary": job.get("salary") or "", "published": job.get("publication_date") or ""},
                published=_iso(job.get("publication_date")),
                facts={"reward_type": _reward_type(job.get("job_type")), "location_requirement": location,
                       "skills_required": _tags(job.get("tags"))},
            )


class RemoteOkCollector(JobBoardCollector):
    kind = "remoteok"
    endpoint = "https://remoteok.com/api"
    option_keys = ("tag",)
    domain = "remoteok.com"
    site_name = "Remote OK"

    def headers(self):
        return {"User-Agent": REMOTEOK_USER_AGENT}

    def collect(self, source):
        payload = self.get(self.endpoint, params=self.params(source)).json()
        jobs = payload[1:] if isinstance(payload, list) else []  # [0] is the legal notice, not a job
        for job in jobs:
            if not isinstance(job, dict) or not job.get("id") or not _on_site(job.get("url"), self.domain):
                continue
            location = str(job.get("location") or "").strip() or None
            salary = _salary(job.get("salary_min"), job.get("salary_max"))
            tags = _tags(job.get("tags"))
            facts = [("Company", job.get("company")), ("Location", location), ("Salary", salary),
                     ("Tags", ", ".join(tags))]
            yield RawOpportunity(
                external_id=f"remoteok:{job['id']}",
                url=job["url"],
                title=_plain(job.get("position") or job["url"])[:500],
                text=_facts_text(job.get("description"), facts),
                metadata={"via": self.site_name, "company": job.get("company") or "", "tags": tags,
                          "salary": salary or "", "published": job.get("date") or ""},
                published=_iso(job.get("date")),
                facts={"location_requirement": location, "skills_required": tags},
            )


def _salary(low, high):
    def amount(v):
        try:
            return int(float(v)) if v not in (None, "") and float(v) > 0 else None
        except (TypeError, ValueError):
            return None
    low, high = amount(low), amount(high)
    if low and high:
        return f"${low:,}–${high:,} per year"
    if low or high:
        return f"${(low or high):,} per year"
    return None


def _iso(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt_timezone.utc)


class HimalayasCollector(JobBoardCollector):
    """https://himalayas.app/jobs/api - credit Himalayas and link back to the job on himalayas.app."""
    kind = "himalayas"
    endpoint = "https://himalayas.app/jobs/api"
    option_keys = ()
    domain = "himalayas.app"
    site_name = "Himalayas"

    def collect(self, source):
        payload = self.get(self.endpoint, params={"limit": 20}).json()
        for job in (payload.get("jobs") if isinstance(payload, dict) else None) or []:
            url = (job or {}).get("guid") or ""
            if not isinstance(job, dict) or not _on_site(url, self.domain):
                continue
            locations = _tags(job.get("locationRestrictions"))
            location = ", ".join(locations) if locations else "Remote (worldwide)"
            salary = _annual_salary(job.get("minSalary"), job.get("maxSalary"), job.get("currency") or "USD")
            facts = [("Company", job.get("companyName")), ("Employment type", job.get("employmentType")),
                     ("Seniority", ", ".join(_tags(job.get("seniority")))), ("Location", location),
                     ("Salary", salary), ("Categories", ", ".join(_tags(job.get("categories"))))]
            yield RawOpportunity(
                external_id=f"himalayas:{url}"[:300],
                url=url,
                title=_plain(job.get("title") or url)[:500],
                text=_facts_text(job.get("description") or job.get("excerpt"), facts),
                metadata={"via": self.site_name, "company": job.get("companyName") or "",
                          "employment_type": job.get("employmentType") or "", "salary": salary or ""},
                published=_from_unix(job.get("pubDate")),
                facts={"reward_type": _reward_type(job.get("employmentType")), "location_requirement": location,
                       "skills_required": _tags(job.get("skills"))},
            )


class JobicyCollector(JobBoardCollector):
    """https://jobicy.com/api/v2/remote-jobs - credit Jobicy and link back to the job on jobicy.com."""
    kind = "jobicy"
    endpoint = "https://jobicy.com/api/v2/remote-jobs"
    option_keys = ("tag", "industry", "geo")
    domain = "jobicy.com"
    site_name = "Jobicy"

    def collect(self, source):
        params = {"count": 50, **(self.params(source) or {})}
        payload = self.get(self.endpoint, params=params).json()
        for job in (payload.get("jobs") if isinstance(payload, dict) else None) or []:
            if not isinstance(job, dict) or not job.get("id") or not _on_site(job.get("url"), self.domain):
                continue
            job_types = job.get("jobType") if isinstance(job.get("jobType"), list) else [job.get("jobType")]
            location = str(job.get("jobGeo") or "").strip() or None
            salary = _annual_salary(job.get("annualSalaryMin"), job.get("annualSalaryMax"), job.get("salaryCurrency"))
            facts = [("Company", job.get("companyName")), ("Job type", ", ".join(map(str, filter(None, job_types)))),
                     ("Level", job.get("jobLevel")), ("Location", location), ("Salary", salary),
                     ("Industry", ", ".join(map(str, job.get("jobIndustry") or [])))]
            yield RawOpportunity(
                external_id=f"jobicy:{job['id']}",
                url=job["url"],
                title=_plain(job.get("jobTitle") or job["url"])[:500],
                text=_facts_text(job.get("jobDescription") or job.get("jobExcerpt"), facts),
                metadata={"via": self.site_name, "company": job.get("companyName") or "", "salary": salary or "",
                          "published": job.get("pubDate") or ""},
                published=_iso(job.get("pubDate")),
                facts={"reward_type": _reward_type(job_types), "location_requirement": location},
            )


class ArbeitnowCollector(JobBoardCollector):
    """https://www.arbeitnow.com/api/job-board-api - credit Arbeitnow and link back to the job on arbeitnow.com."""
    kind = "arbeitnow"
    endpoint = "https://www.arbeitnow.com/api/job-board-api"
    option_keys = ()
    domain = "arbeitnow.com"
    site_name = "Arbeitnow"

    def collect(self, source):
        payload = self.get(self.endpoint).json()
        for job in (payload.get("data") if isinstance(payload, dict) else None) or []:
            if not isinstance(job, dict) or not job.get("slug") or not _on_site(job.get("url"), self.domain):
                continue
            location = "Remote" if job.get("remote") else (str(job.get("location") or "").strip() or None)
            job_types = _tags(job.get("job_types"))
            facts = [("Company", job.get("company_name")), ("Job type", ", ".join(job_types)),
                     ("Location", job.get("location")), ("Remote", "yes" if job.get("remote") else "no"),
                     ("Tags", ", ".join(_tags(job.get("tags"))))]
            yield RawOpportunity(
                external_id=f"arbeitnow:{job['slug']}"[:300],
                url=job["url"],
                title=_plain(job.get("title") or job["url"])[:500],
                text=_facts_text(job.get("description"), facts),
                metadata={"via": self.site_name, "company": job.get("company_name") or "", "remote": bool(job.get("remote"))},
                published=_from_unix(job.get("created_at")),
                facts={"reward_type": _reward_type(job_types), "location_requirement": location,
                       "skills_required": _tags(job.get("tags"))},
            )


ENDPOINTS = {"remotive": (RemotiveCollector.endpoint, RemotiveCollector.option_keys),
             "remoteok": (RemoteOkCollector.endpoint, RemoteOkCollector.option_keys),
             "himalayas": (HimalayasCollector.endpoint, HimalayasCollector.option_keys),
             "jobicy": (JobicyCollector.endpoint, JobicyCollector.option_keys),
             "arbeitnow": (ArbeitnowCollector.endpoint, ArbeitnowCollector.option_keys)}
