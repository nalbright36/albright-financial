"""What belongs in the Inbox - in two stages, to keep AI cost down:

  1. A free prefilter (Settings > Filters): work types, negative keywords,
     seniority, location and pay floors. Anything failing goes to Hidden
     with the reason ("location: UK only", "pay below floor: ...",
     "negative keyword: nurse").
  2. A fit score (0-100) for job and freelance postings that pass: the AI
     compares the posting with My profile (ai_tasks.run_json, purpose "fit",
     within the daily AI limit and shared budget). Jobs from the job boards
     whose terms keep their jobs in this app (Source.keep_in_app - Remotive,
     Remote OK, Himalayas, Jobicy, Arbeitnow) are never sent to the AI; they
     get a keyword fit score from the same profile instead. Below
     fit_min_score (60) -> Hidden ("low fit").

Bounties, paid tasks and crypto keep the EV/hour score (core.scoring) and
have no fit score. Also here: deriving work type / company, merging the same
posting found on several sources, and auto-expiring stale Inbox items.
"""
import logging
import math
import re
from datetime import timedelta
from difflib import SequenceMatcher

from django.db.models import Q
from django.db.models.functions import Coalesce
from django.utils import timezone

from . import config
from .skills import normalize

log = logging.getLogger(__name__)

JOB_WORK_TYPES = ("full_time", "part_time", "contract", "freelance")   # get a fit score; the location/pay/seniority filters apply
FILTERABLE_WORK_TYPES = ("full_time", "contract", "freelance", "part_time", "bounty", "paid_task")
WORK_TYPE_LABELS = {"full_time": "Full-time", "part_time": "Part-time", "contract": "Contract", "freelance": "Freelance",
                    "bounty": "Bounty", "paid_task": "Paid task", "crypto": "Crypto", "other": "Other"}

# The Inbox's Type filter.
TYPE_FILTERS = [("all", "All"), ("jobs", "Jobs"), ("freelance", "Freelance"), ("bounties", "Bounties"),
                ("paid_tasks", "Paid tasks"), ("crypto", "Crypto")]
TYPE_FILTER_Q = {
    "jobs": Q(work_type__in=("full_time", "part_time")),
    "freelance": Q(work_type__in=("contract", "freelance")),
    "bounties": Q(work_type="bounty"),
    "paid_tasks": Q(work_type="paid_task"),
    "crypto": Q(opportunity_type="crypto"),
}


def type_filter(qs, key):
    return qs.filter(TYPE_FILTER_Q[key]) if key in TYPE_FILTER_Q else qs


# ---------------------------------------------------------------------------
# Work type, company
# ---------------------------------------------------------------------------

STATED_TYPES = [   # first match wins
    ("contract", re.compile(r"\bcontract(or)?\b|\bc2h\b|contract[- ]to[- ]hire", re.I)),
    ("freelance", re.compile(r"\bfreelanc", re.I)),
    ("part_time", re.compile(r"\bpart[-_ ]?time\b", re.I)),
    ("full_time", re.compile(r"\bfull[-_ ]?time\b|\bpermanent\b|\binternship\b", re.I)),
]
BOARD_KINDS = ("remotive", "remoteok", "himalayas", "jobicy", "arbeitnow", "hn_hiring", "email", "rss_income",
               "api_income")


def _stated_job_type(opp):
    meta = opp.metadata or {}
    stated = " ".join(str(v) for v in [meta.get("job_type"), meta.get("employment_type"),
                                       " ".join(meta.get("work_types") or [])] if v)
    for text in (stated, opp.title, (opp.raw_text or "")[:800]):
        for work_type, pattern in STATED_TYPES:
            if text and pattern.search(text):
                return work_type
    return None


def derive_work_type(opp):
    if opp.opportunity_type == "crypto":
        return "bounty" if opp.reward_type == "bounty" else "crypto"
    if opp.reward_type == "bounty":
        return "bounty"
    if opp.reward_type in ("paid_task", "research_study"):
        return "paid_task"
    if opp.reward_type in ("competition", "bonus", "settlement"):
        return "other"
    stated = _stated_job_type(opp)
    if stated:
        return stated
    if opp.reward_type == "freelance" or opp.pay_type == "fixed":
        return "freelance"
    if opp.pay_type == "hourly":
        return "contract"
    return "full_time"   # a job posting that doesn't say otherwise


def derive_company(opp):
    meta = opp.metadata or {}
    if str(meta.get("company") or "").strip():
        return str(meta["company"]).strip()[:200]
    kind = opp.source.kind if opp.source_id else ""
    if kind == "hn_hiring":   # "Company - role" / "Company | role"
        head = re.split(r"\s+[|\-–]\s+", opp.title, maxsplit=1)[0].strip()
        return head[:200] if head != opp.title.strip() else ""
    if kind == "rss_income" and ":" in opp.title:   # We Work Remotely: "Company: Role"
        return opp.title.split(":", 1)[0].strip()[:200]
    return ""


def derive(opp):
    """Sets work_type and company (company only if not already known). Doesn't save."""
    opp.work_type = derive_work_type(opp)
    if not opp.company:
        opp.company = derive_company(opp)
    return opp


def posted(opp):
    return opp.posted_at or opp.discovered_at


# ---------------------------------------------------------------------------
# Stage 1: the free prefilter
# ---------------------------------------------------------------------------

def _phrase_re(phrase):
    return re.compile(r"(?<![\w.])" + re.escape(phrase.strip()) + r"(?!\w)", re.I)


def negative_keyword(opp, keywords):
    """Single words are matched in the title only ("driver" in "a key driver
    of growth" isn't a driving job); phrases also in the posting text."""
    for keyword in keywords or []:
        keyword = str(keyword).strip()
        if not keyword:
            continue
        pattern = _phrase_re(keyword)
        if pattern.search(opp.title) or (" " in keyword and pattern.search(opp.raw_text or "")):
            return keyword
    return None


ENTRY_RE = re.compile(r"\b(intern|internship|entry[- ]level|new[- ]grad(uate)?|graduate program|apprentice(ship)?|"
                      r"trainee)\b", re.I)
EXEC_RE = re.compile(r"\b(vp|svp|evp|avp|vice[- ]president|chief [a-z]+ officer|ceo|cto|cfo|coo|cio|cdo|cmo|cpo|"
                     r"c-level|c-suite|president)\b", re.I)


def seniority_problem(opp, flt):
    meta = opp.metadata or {}
    text = " ".join(filter(None, [opp.title, str(meta.get("seniority") or ""), str(meta.get("level") or "")]))
    if flt.get("exclude_entry") and (ENTRY_RE.search(text) or opp.seniority in ("intern", "entry")):
        return "intern / entry-level"
    if flt.get("exclude_executive") and (EXEC_RE.search(text) or opp.seniority == "executive"):
        return "VP / C-level"
    return None


# --- Location ---------------------------------------------------------------

# Towns around Tampa Bay and the rest of Florida, for "within N miles of home".
PLACES = {
    "tampa": (27.9506, -82.4572), "st petersburg": (27.7676, -82.6403), "st pete": (27.7676, -82.6403),
    "clearwater": (27.9659, -82.8001), "brandon": (27.9378, -82.2859), "riverview": (27.8661, -82.3265),
    "wesley chapel": (28.2397, -82.3279), "lutz": (28.1511, -82.4615), "largo": (27.9095, -82.7873),
    "pinellas park": (27.8428, -82.6995), "temple terrace": (28.0353, -82.3893), "plant city": (28.0186, -82.1129),
    "oldsmar": (28.0342, -82.6651), "palm harbor": (28.0781, -82.7637), "dunedin": (28.0197, -82.7718),
    "seminole": (27.8397, -82.7912), "land o lakes": (28.2189, -82.4576), "apollo beach": (27.7731, -82.4076),
    "ruskin": (27.7209, -82.4332), "valrico": (27.9370, -82.2365), "odessa": (28.1939, -82.5915),
    "safety harbor": (27.9908, -82.6929), "tarpon springs": (28.1461, -82.7568), "new port richey": (28.2442, -82.7193),
    "port richey": (28.2717, -82.7195), "zephyrhills": (28.2336, -82.1812), "carrollwood": (28.0500, -82.4926),
    "westchase": (28.0550, -82.6098), "town n country": (28.0106, -82.5773), "sun city center": (27.7181, -82.3518),
    "gibsonton": (27.8536, -82.3826), "seffner": (27.9836, -82.2756), "thonotosassa": (28.0614, -82.3023),
    "trinity": (28.1806, -82.6815), "holiday": (28.1878, -82.7395), "lakeland": (28.0395, -81.9498),
    "bradenton": (27.4989, -82.5748), "palmetto": (27.5214, -82.5723), "sarasota": (27.3364, -82.5307),
    "spring hill": (28.4769, -82.5254), "brooksville": (28.5553, -82.3879), "winter haven": (28.0222, -81.7329),
    "orlando": (28.5384, -81.3789), "kissimmee": (28.2920, -81.4076), "ocala": (29.1872, -82.1401),
    "gainesville": (29.6516, -82.3248), "jacksonville": (30.3322, -81.6557), "tallahassee": (30.4383, -84.2807),
    "daytona beach": (29.2108, -81.0228), "fort myers": (26.6406, -81.8723), "naples": (26.1420, -81.7948),
    "miami": (25.7617, -80.1918), "fort lauderdale": (26.1224, -80.1373), "west palm beach": (26.7153, -80.0534),
    "boca raton": (26.3683, -80.1289),
}
NON_US = [   # (label, pattern) - a location naming only these (and no US) is restricted elsewhere
    ("UK", r"uk|u\.k\.|united kingdom|england|scotland|wales|britain|london|manchester"),
    ("Europe", r"europe|european|eu|emea|eea|cet|cest"),
    ("Germany", r"germany|deutschland|berlin|munich|hamburg|frankfurt"),
    ("Canada", r"canada|toronto|vancouver|montreal|ottawa|calgary"),
    ("India", r"india|bangalore|bengaluru|hyderabad|pune|chennai|delhi|mumbai"),
    ("Latin America", r"latam|latin america|south america|brazil|argentina|mexico|colombia|chile|peru"),
    ("Asia-Pacific", r"apac|asia|australia|new zealand|singapore|philippines|japan|vietnam|indonesia|malaysia"),
    ("Africa", r"africa|nigeria|kenya|egypt"),
    ("Middle East", r"israel|uae|dubai|saudi|turkey"),
    ("Europe", r"france|paris|spain|madrid|barcelona|netherlands|amsterdam|ireland|dublin|poland|portugal|lisbon|"
               r"italy|sweden|switzerland|zurich|austria|belgium|denmark|norway|finland|ukraine|romania|czech"),
    ("Pakistan", r"pakistan"),
]
NON_US = [(label, re.compile(rf"\b({pattern})\b", re.I)) for label, pattern in NON_US]
US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH "
             "OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
US_RE = re.compile(r"\b(us|usa|u\.s\.a?\.?|united states|america|americas|north america|worldwide|anywhere|global|"
                   r"globally)\b", re.I)
US_STATE_RE = re.compile(r"\b(?:%s)\b" % "|".join(US_STATES))   # upper case only: "in", "or", "me" aren't states
STATE_RE = re.compile(r",\s*([A-Z]{2})\b")
# Big US job markets outside Florida, written without a state ("Onsite NYC").
OTHER_US_CITIES = re.compile(r"\b(nyc|new york|manhattan|brooklyn|san francisco|sf|bay area|silicon valley|"
                             r"palo alto|mountain view|san jose|oakland|seattle|bellevue|redmond|portland|austin|"
                             r"dallas|houston|denver|boulder|boston|cambridge|chicago|los angeles|la|san diego|"
                             r"atlanta|washington dc|dc|philadelphia|pittsburgh|detroit|minneapolis|phoenix|"
                             r"salt lake city|nashville|charlotte|raleigh|las vegas)\b", re.I)


def _place_key(text):
    text = text.lower().replace("saint ", "st ").replace("st. ", "st ").replace("'", "").replace("’", "")
    return re.sub(r"[^a-z ]+", " ", text)


def find_place(text):
    key = f" {_place_key(text)} "
    for name in sorted(PLACES, key=len, reverse=True):
        if f" {name} " in re.sub(r"\s+", " ", key):
            return name
    return None


def miles_between(a, b):
    (lat1, lon1), (lat2, lon2) = PLACES[a], PLACES[b]
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def _is_remote(opp, location):
    meta = opp.metadata or {}
    return bool(re.search(r"\bremote\b|\banywhere\b|\bworldwide\b", location, re.I) or meta.get("remote") is True)


def location_problem(opp, flt):
    location = (opp.location_requirement or "").strip()
    if not location:
        return None   # not stated - can't tell, so it stays
    has_us = bool(US_RE.search(location) or US_STATE_RE.search(location))
    if not has_us:
        for label, pattern in NON_US:
            if pattern.search(location):
                return f"location: {label} only"
    home_text = str(flt.get("home") or "")
    home = find_place(home_text)
    if _is_remote(opp, location):   # "Remote (US) or hybrid in NYC" is still open to me remotely
        return None if flt.get("remote_us") else "location: remote (switched off in Filters)"
    # Hybrid / on-site: near home, or not at all.
    place = find_place(location)
    radius = float(flt.get("radius_miles") or 0)
    if place:
        near = (miles_between(home, place) <= radius) if home else place == _place_key(home_text.split(",")[0]).strip()
        if near:
            return None if flt.get("local") else "location: on-site / hybrid (switched off in Filters)"
        return f"location: on-site in {location[:60]} (not within {radius:g} miles of {home_text})"
    state = STATE_RE.search(location)
    home_state = STATE_RE.search(home_text)
    elsewhere = (state and state.group(1) in US_STATES and state.group(1) != (home_state.group(1) if home_state else None))
    if elsewhere or OTHER_US_CITIES.search(location):
        return f"location: on-site in {location[:60]} (not within {radius:g} miles of {home_text})"
    return None   # a place we can't measure (e.g. "Hybrid", an unlisted Florida town) - left for me to judge


# --- Pay --------------------------------------------------------------------

MONEY_RE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kK])?")
HOURLY_RE = re.compile(r"/\s?h(ou)?r\b|/\s?h\b|per hour|an hour|hourly", re.I)
YEARLY_RE = re.compile(r"/\s?y(ea)?r\b|per year|a year|annual|annum|salary|/\s?yr", re.I)
HOURS_PER_YEAR = 2080


def stated_pay(opp):
    """(top of the stated pay, "hour" | "year" | "fixed") or None. Only US
    dollar amounts count; the top of a range is compared with the floor, so a
    posting is only hidden when even its best case is below it."""
    if opp.pay_rate_usd is not None and opp.pay_type in ("hourly", "fixed"):
        return float(opp.pay_rate_usd), "hour" if opp.pay_type == "hourly" else "fixed"
    if opp.pay_type == "fixed" and opp.reward_usd is not None:
        return float(opp.reward_usd), "fixed"
    salary = str((opp.metadata or {}).get("salary") or "")
    amounts = [float(n.replace(",", "")) * (1000 if k else 1) for n, k in MONEY_RE.findall(salary)]
    if not amounts:
        return None
    top = max(amounts)
    if HOURLY_RE.search(salary):
        return top, "hour"
    if YEARLY_RE.search(salary) or top >= 1000:
        return top, "year"
    return top, "hour"


def pay_problem(opp, flt):
    pay = stated_pay(opp)
    if pay is None:
        return None
    amount, period = pay
    if opp.work_type == "full_time":
        floor = float(flt.get("min_salary_year") or 0)
        yearly = amount * HOURS_PER_YEAR if period == "hour" else amount
        if period != "fixed" and yearly < floor:
            return f"pay below floor: ${amount:,.0f}/{period} (floor ${floor:,.0f}/year)"
        return None
    if period == "fixed":
        floor = float(flt.get("min_fixed_project") or 0)
        return f"pay below floor: ${amount:,.0f} fixed (floor ${floor:,.0f})" if amount < floor else None
    floor = float(flt.get("min_rate_hour") or 0)
    hourly = amount / HOURS_PER_YEAR if period == "year" else amount
    return f"pay below floor: ${amount:,.0f}/{period} (floor ${floor:,.0f}/hour)" if hourly < floor else None


def prefilter(opp, flt=None):
    """The first reason this opportunity is filtered out, or "" (stage 1 - free)."""
    flt = flt or config.filters()
    if opp.work_type in FILTERABLE_WORK_TYPES and opp.work_type not in (flt.get("work_types") or []):
        return f"work type: {WORK_TYPE_LABELS[opp.work_type]} (switched off in Filters)"
    if opp.opportunity_type == "crypto":
        return ""
    keyword = negative_keyword(opp, flt.get("negative_keywords"))
    if keyword:
        return f"negative keyword: {keyword}"
    if opp.work_type not in JOB_WORK_TYPES:
        return ""
    seniority = seniority_problem(opp, flt)
    if seniority:
        return f"seniority: {seniority}"
    return location_problem(opp, flt) or pay_problem(opp, flt) or ""


def apply_hidden(opp, flt=None, min_fit=None):
    """Sets hidden_reason from the prefilter, then the fit score. Doesn't save."""
    if opp.show_anyway:
        opp.hidden_reason = ""
        return opp
    reason = prefilter(opp, flt)
    min_fit = config.fit_min_score() if min_fit is None else min_fit
    if not reason and opp.work_type in JOB_WORK_TYPES and opp.fit_score is not None and opp.fit_score < min_fit:
        reason = f"low fit ({opp.fit_score}/100)"
    opp.hidden_reason = reason[:200]
    return opp


# ---------------------------------------------------------------------------
# Stage 2: the fit score
# ---------------------------------------------------------------------------

SENIORITIES = ("intern", "entry", "mid", "senior", "lead", "manager", "director", "executive", "unknown")

FIT_PROMPT = """You compare ONE job posting with a candidate's profile and score how well it fits, 0-100.
Use ONLY what the posting and the profile say. Never invent requirements, skills or experience.
Scoring guide: 80-100 = the role is one of the target roles (or very close) and most of what it asks for is in the
profile; 60-79 = a related role with a good share of the skills; 40-59 = partly related; below 40 = a different field.
Return ONLY a JSON object:
{"fit_score": integer 0-100,
 "matched_skills": [skills from the PROFILE'S SKILLS list that the posting asks for - spelled as in the profile],
 "missing_requirements": [the posting's requirements the profile doesn't show - short phrases, at most 8],
 "role_match": the closest TARGET ROLE exactly as written in the profile, or "" if none is close,
 "seniority": one of "intern","entry","mid","senior","lead","manager","director","executive","unknown" (as the posting states),
 "company": the hiring company's name if the posting states it, else "",
 "why_it_fits": one sentence}"""


# For collectors that already make one AI call per batch of postings (HN,
# email alerts): the same fit fields, asked for in that call - no extra calls.
BATCH_FIT_RULES = """The CANDIDATE PROFILE comes first: also score how well each posting fits it, 0-100 (80-100 = one of
the target roles, or very close, with most of what it asks for in the profile; 60-79 = a related role with a good
share of the skills; 40-59 = partly related; below 40 = a different field)."""
BATCH_FIT_FIELDS = """  "fit_score": integer 0-100,
  "matched_skills": [skills from the PROFILE'S SKILLS list that the posting asks for - spelled as in the profile],
  "missing_requirements": [the posting's requirements the profile doesn't show - short, at most 8],
  "role_match": the closest TARGET ROLE exactly as written in the profile, or "",
  "seniority": one of "intern","entry","mid","senior","lead","manager","director","executive","unknown",
  "why_it_fits": one sentence,"""


def profile_brief(profile):
    lines = [
        f"TARGET ROLES: {'; '.join(profile.target_roles or []) or 'not set'}",
        f"SKILLS: {', '.join(profile.skills or []) or 'not set'}",
        f"YEARS OF EXPERIENCE: {profile.years_experience or 'not stated'}",
        f"INDUSTRIES: {', '.join(profile.industries or []) or 'not stated'}",
        f"LOCATION: {profile.location or 'not stated'}",
        "EDUCATION: " + ("; ".join(" ".join(filter(None, [e.get("degree"), e.get("school"), str(e.get("year") or "")]))
                                   for e in profile.education or []) or "not stated"),
        f"CERTIFICATIONS: {', '.join(profile.certifications or []) or 'none'}",
        "WORK HISTORY: " + ("; ".join(f"{j.get('title')} at {j.get('employer')}" for j in profile.work_history or [])
                            or "not stated"),
    ]
    if profile.summary:
        lines.append(f"SUMMARY: {profile.summary}")
    return "\n".join(lines)


def _posting_brief(opp):
    pay = stated_pay(opp)
    return "\n".join([
        f"TITLE: {opp.title}",
        f"COMPANY: {opp.company or 'not stated'}",
        f"WORK TYPE: {WORK_TYPE_LABELS.get(opp.work_type, opp.work_type)}",
        f"LOCATION: {opp.location_requirement or 'not stated'}",
        f"PAY: {f'${pay[0]:,.0f}/{pay[1]}' if pay else 'not stated'}",
        f"SKILLS LISTED: {', '.join(opp.skills_required or []) or 'none'}",
        "", (opp.raw_text or "")[:6000],
    ])


def _profile_skill(name, profile):
    key = normalize(name)
    return next((s for s in profile.skills or [] if normalize(s) == key), None)


def validate_fit(data, profile):
    """Only what can be checked survives: skills must be in my profile,
    role_match one of my target roles, seniority from the fixed list."""
    try:
        fit_score = max(0, min(100, int(round(float(data.get("fit_score"))))))
    except (TypeError, ValueError):
        fit_score = None
    matched = []
    for skill in data.get("matched_skills") or []:
        mine = _profile_skill(skill, profile) if isinstance(skill, str) else None
        if mine and mine not in matched:
            matched.append(mine)
    role = str(data.get("role_match") or "").strip().lower()
    role_match = next((r for r in profile.target_roles or [] if r.lower() == role), "")
    seniority = str(data.get("seniority") or "").strip().lower()
    return {
        "fit_score": fit_score,
        "matched_skills": matched[:20],
        "missing_requirements": [str(x).strip()[:150] for x in data.get("missing_requirements") or []
                                 if str(x).strip()][:8],
        "role_match": role_match[:120],
        "seniority": seniority if seniority in SENIORITIES else "unknown",
        "fit_reason": str(data.get("why_it_fits") or "").strip()[:300],
        "company": str(data.get("company") or "").strip()[:200],
    }


def ai_fit(opp, profile, client=None):
    from .ai_tasks import run_json
    data, _cost = run_json(FIT_PROMPT, f"PROFILE:\n{profile_brief(profile)}\n\nPOSTING:\n{_posting_brief(opp)}",
                           "fit", opportunity=opp, client=client, max_tokens=600)
    return validate_fit(data, profile)


def role_variants(role):
    """"Business Intelligence Analyst/Developer" -> both titles (and "BI ..."),
    "Python/Django Developer (freelance/contract)" -> "Python Developer", "Django Developer"."""
    words = re.sub(r"\(.*?\)", "", role).split()
    variants = [[]]
    for word in words:
        variants = [v + [alt] for v in variants for alt in word.split("/") if alt]
    names = {" ".join(v).lower() for v in variants}
    return names | {n.replace("business intelligence", "bi") for n in names if "business intelligence" in n}


ROLE_WORDS = re.compile(r"\b(data|analyst|analytics|bi|business intelligence|power bi|tableau|reporting|salesforce|"
                        r"django|python|etl|dashboard|insights)\b", re.I)
SENIORITY_WORDS = [("executive", EXEC_RE), ("intern", re.compile(r"\bintern(ship)?\b", re.I)),
                   ("entry", re.compile(r"\b(entry[- ]level|junior|jr\.?)\b", re.I)),
                   ("director", re.compile(r"\bdirector\b", re.I)), ("manager", re.compile(r"\bmanager\b", re.I)),
                   ("lead", re.compile(r"\b(lead|principal|staff)\b", re.I)),
                   ("senior", re.compile(r"\b(senior|sr\.?)\b", re.I))]


def keyword_fit(opp, profile):
    """A fit score without the AI (for job boards whose terms keep their jobs
    here): 60 for a target role in the title - enough to reach the Inbox on its
    own - (30 for a related title), plus 9 per profile skill the posting
    mentions, up to 100."""
    title = opp.title.lower()
    text = f"{opp.title}\n{' '.join(opp.skills_required or [])}\n{opp.raw_text or ''}"
    # The most specific target role in the title wins ("Power BI Developer" over "BI Developer").
    hits = [(max(len(v) for v in role_variants(r) if v in title), r) for r in profile.target_roles or []
            if any(v in title for v in role_variants(r))]
    role_match = max(hits)[1] if hits else ""
    listed = {normalize(s) for s in opp.skills_required or []}
    matched = [s for s in profile.skills or []
               if normalize(s) in listed or _phrase_re(re.sub(r"\s*\(.*?\)", "", s)).search(text)]
    base = 60 if role_match else (30 if ROLE_WORDS.search(opp.title) else 0)
    score = min(100, base + 9 * len(matched))
    mine = {normalize(s) for s in profile.skills or []}
    missing = [s for s in opp.skills_required or [] if normalize(s) not in mine][:8]
    seniority = next((name for name, pattern in SENIORITY_WORDS if pattern.search(opp.title)), "mid")
    why = (f"Title matches {role_match}" if role_match else "No target role in the title") + (
        f"; mentions {', '.join(matched[:5])}{' and more' if len(matched) > 5 else ''}." if matched else "; none of your skills mentioned.")
    return {"fit_score": score, "matched_skills": matched[:20], "missing_requirements": missing,
            "role_match": role_match, "seniority": seniority, "company": "",
            "fit_reason": f"Keyword match (this board's jobs aren't sent to the AI): {why}"[:300]}


def score_fit(opp, use_ai=True, client=None, profile=None):
    """Stage 2 for a job/freelance posting that passed the prefilter and has
    no fit score yet. Saves nothing; returns True if a score was set. An AI
    limit or failure leaves it unscored ("fit pending") for a later scan."""
    from .extraction import ExtractionLimitReached
    from .models import MyProfile
    if opp.work_type not in JOB_WORK_TYPES or opp.fit_method or opp.duplicate_of_id or prefilter(opp):
        return False
    profile = profile or MyProfile.load()
    if not (profile.target_roles or profile.skills):
        return False   # nothing to compare with yet - scored once the Profile is filled in
    if opp.source.keep_in_app:
        result, method = keyword_fit(opp, profile), "keywords"
    elif use_ai:
        try:
            result, method = ai_fit(opp, profile, client=client), "ai"
        except ExtractionLimitReached as exc:
            log.info("Fit score for %s waits: %s", opp.pk, exc)
            return False
        except Exception:  # noqa: BLE001 - recorded as an AIExtraction error row
            return False
        if result["fit_score"] is None:
            return False
    else:
        return False
    for field in ("fit_score", "matched_skills", "missing_requirements", "role_match", "seniority", "fit_reason"):
        setattr(opp, field, result[field])
    if result["company"] and not opp.company:
        opp.company = result["company"]
    opp.fit_method = method
    return True


def score_pending_fits(use_ai=True, client=None, limit=50):
    """Inbox postings still waiting for a fit score (AI limit reached earlier,
    or filters loosened) - oldest first, until the AI limit stops it."""
    from .models import MyProfile, Opportunity
    profile = MyProfile.load()
    scored = 0
    pending = (Opportunity.objects.select_related("source")
               .filter(status="inbox", fit_method="", duplicate_of__isnull=True, hidden_reason="",
                       work_type__in=JOB_WORK_TYPES, excluded=False)
               .exclude(safety_verdict="block").order_by("discovered_at")[:limit])
    ai_available = use_ai
    for opp in pending:
        if not opp.source.keep_in_app and not ai_available:
            continue   # AI limit reached - the keyword-scored boards can still go
        if score_fit(opp, use_ai=ai_available, client=client, profile=profile):
            apply_hidden(opp)
            opp.save()
            scored += 1
        elif not opp.source.keep_in_app:
            ai_available = False
    return scored


# ---------------------------------------------------------------------------
# Duplicates and expiry
# ---------------------------------------------------------------------------

DUPLICATE_WINDOW = timedelta(days=30)
COMPANY_SUFFIXES = re.compile(r"\b(inc|llc|ltd|limited|corp|corporation|co|company|gmbh|plc|sa|ag|bv)\b")


def norm_company(name):
    name = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", COMPANY_SUFFIXES.sub(" ", name)).strip()


def norm_title(title):
    title = re.sub(r"\(.*?\)|\[.*?\]", " ", (title or "").lower())
    title = re.sub(r"\bsr\b\.?", "senior", title)
    title = re.sub(r"[^a-z0-9 ]+", " ", title)
    return re.sub(r"\s+", " ", re.sub(r"\b(remote|hybrid|us|usa|m f d|f m d)\b", " ", title)).strip()


def similar_titles(a, b):
    a, b = norm_title(a), norm_title(b)
    return bool(a and b) and (a == b or SequenceMatcher(None, a, b).ratio() >= 0.85)


def _bare_title(opp):
    """The role part of the title ("Acme: Data Analyst" -> "Data Analyst")."""
    title = opp.title
    if opp.company and title.lower().startswith(opp.company.lower()):
        title = re.sub(r"^[\s:|\-–]+", "", title[len(opp.company):])
    return title


def find_duplicate(opp):
    """An earlier posting from another source with the same company and a
    similar title, posted within 30 days of this one."""
    from .models import Opportunity
    company = norm_company(opp.company)
    if not company:
        return None
    when = posted(opp)
    candidates = (Opportunity.objects.select_related("source")
                  .filter(duplicate_of__isnull=True, discovered_at__gte=when - DUPLICATE_WINDOW)
                  .exclude(pk=opp.pk).exclude(source=opp.source_id).exclude(company="").order_by("discovered_at"))
    for other in candidates:
        if (norm_company(other.company) == company and abs(posted(other) - when) <= DUPLICATE_WINDOW
                and similar_titles(_bare_title(other), _bare_title(opp))):
            return other
    return None


def source_label(opp):
    return opp.source.attribution[0] if opp.source.attribution else opp.source.name


def merge_into(duplicate, primary):
    """The duplicate disappears into the primary, which keeps its link."""
    from .models import Opportunity
    links = list(primary.other_links or [])
    if not any(link.get("url") == duplicate.url for link in links) and duplicate.url != primary.url:
        links.append({"source": source_label(duplicate), "url": duplicate.url, "opportunity": duplicate.pk})
    for link in duplicate.other_links or []:   # anything already merged into the duplicate moves too
        if link.get("url") != primary.url and not any(l.get("url") == link.get("url") for l in links):
            links.append(link)
    primary.other_links = links
    Opportunity.objects.filter(pk=primary.pk).update(other_links=links)
    Opportunity.objects.filter(duplicate_of=duplicate).update(duplicate_of=primary)
    duplicate.duplicate_of = primary
    duplicate.other_links = []
    Opportunity.objects.filter(pk=duplicate.pk).update(duplicate_of=primary, other_links=[])


def merge_duplicates():
    """Sweep: every not-yet-merged posting checked against earlier ones."""
    from .models import Opportunity
    merged = 0
    for opp in (Opportunity.objects.select_related("source").filter(duplicate_of__isnull=True)
                .exclude(company="").exclude(source__kind="manual").order_by("discovered_at")):
        opp.refresh_from_db(fields=["duplicate_of"])
        if opp.duplicate_of_id:
            continue
        primary = find_duplicate(opp)
        if primary is not None and primary.discovered_at <= opp.discovered_at:
            merge_into(opp, primary)
            merged += 1
    return merged


INBOX_MAX_AGE = timedelta(days=14)
FULL_TIME_MAX_AGE = timedelta(days=30)


def expire_stale(now=None):
    """Inbox items whose deadline has passed, or posted more than 14 days ago
    (30 for full-time), are closed as expired. Returns how many."""
    from .models import Opportunity
    now = now or timezone.now()
    inbox = Opportunity.objects.filter(status="inbox").annotate(posted_on=Coalesce("posted_at", "discovered_at"))
    stale = inbox.filter(
        Q(deadline__lt=now) | Q(application_deadline__lt=now)
        | (Q(posted_on__lt=now - INBOX_MAX_AGE) & ~Q(work_type="full_time"))
        | Q(posted_on__lt=now - FULL_TIME_MAX_AGE, work_type="full_time"))
    return Opportunity.objects.filter(pk__in=list(stale.values_list("pk", flat=True))).update(
        status="closed", close_reason="expired", stage_changed_at=now)


def refresh_inbox(now=None):
    """Free upkeep (every scan, and after Filters change): work type, company
    and Hidden reasons recomputed for everything in the Inbox, duplicates
    merged, stale items expired."""
    from .models import Opportunity
    flt, min_fit = config.filters(), config.fit_min_score()
    changed = []
    for opp in Opportunity.objects.select_related("source").filter(status="inbox", duplicate_of__isnull=True):
        before = (opp.work_type, opp.company, opp.hidden_reason)
        derive(opp)
        apply_hidden(opp, flt, min_fit)
        if (opp.work_type, opp.company, opp.hidden_reason) != before:
            changed.append(opp)
    Opportunity.objects.bulk_update(changed, ["work_type", "company", "hidden_reason"], batch_size=200)
    return {"updated": len(changed), "merged": merge_duplicates(), "expired": expire_stale(now)}
