"""'Walk me through it': a saved, step-by-step plan for one opportunity.

GitHub bounties: the issue, its comments, the repo README and
CONTRIBUTING, the language breakdown, its timeline (for linked pull
requests) and up to MAX_FILES files the issue mentions are READ through the
GitHub REST API (GET only - file contents arrive as text in the API
response). Nothing is cloned or downloaded as an archive, and no repository
code is ever run, imported or executed - it's only shown to the AI as text.

Whether a bounty is still claimable (closed, already assigned, an open PR)
is worked out here from the API data, not by the AI. Payout terms are only
kept as an exact quote from the issue. For crypto, any step that touches a
wallet is always marked manual. Every call uses the shared AI limits/budget
(ai_tasks.run_json).
"""
import re
from urllib.parse import quote, urlparse

import requests
from django.conf import settings

from .ai_tasks import run_json
from .extraction import quote_in_text
from .models import TaskStep, Walkthrough

GITHUB_API = "https://api.github.com"
TIMEOUT = 20
MAX_FILES = 5
LIMITS = {"issue": 8000, "comment": 1500, "comments": 20, "readme": 6000, "contributing": 4000, "file": 3000}
CONTRIBUTING_PATHS = ("CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", "CONTRIBUTING.rst",
                      "CONTRIBUTING")
FILE_EXTENSIONS = ("py|js|jsx|ts|tsx|go|rs|rb|java|kt|swift|c|h|cc|cpp|hpp|cs|php|scala|sol|vue|svelte|css|scss|"
                   "html|md|rst|yml|yaml|toml|json|sh|sql|lua|ex|exs|dart")
PATH_RE = re.compile(rf"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.(?:{FILE_EXTENSIONS}))(?![\w/])")
WALLET_RE = re.compile(r"wallet|seed|private key|sign(?:ing)?\b|connect (?:your )?wallet|approve|metamask|"
                       r"phantom|ledger|transaction|gas fee|bridge|stake|swap", re.I)
MAX_STEPS = 25


class WalkthroughError(Exception):
    """Shown as a message - e.g. GitHub unreachable."""


# ---------------------------------------------------------------------------
# GitHub context (read-only GETs)
# ---------------------------------------------------------------------------

def github_issue_ref(url):
    """https://github.com/owner/repo/issues/123 -> ("owner", "repo", 123), else None."""
    parsed = urlparse(url or "")
    if (parsed.hostname or "").lower() not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 4 and parts[2] == "issues" and parts[3].isdigit():
        return parts[0], parts[1], int(parts[3])
    return None


class GitHubReader:
    """GET-only GitHub REST API access (GITHUB_TOKEN, if set, only raises
    the rate limit). Never fetches tarballs/zipballs or clones anything."""

    def __init__(self, owner, repo):
        self.base = f"{GITHUB_API}/repos/{quote(owner)}/{quote(repo)}"

    def _headers(self, raw=False):
        headers = {"Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json",
                   "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "AlbrightOpportunities/1.0 (read-only)"}
        token = getattr(settings, "GITHUB_TOKEN", "")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def get(self, path, raw=False, params=None, optional=False):
        resp = requests.get(f"{self.base}{path}", headers=self._headers(raw), params=params, timeout=TIMEOUT)
        if optional and resp.status_code == 404:
            return None
        if resp.status_code in (403, 429):
            raise WalkthroughError("GitHub rate limit reached - try again later (or set GITHUB_TOKEN).")
        if resp.status_code >= 400:
            raise WalkthroughError(f"GitHub answered {resp.status_code} for {path}.")
        return resp.text if raw else resp.json()


def mentioned_paths(*texts):
    """File paths an issue mentions (in text or github.com/.../blob/<ref>/<path> links), first MAX_FILES."""
    found = []
    for text in texts:
        for match in re.finditer(r"github\.com/[^/\s]+/[^/\s]+/blob/[^/\s]+/([^\s)#?\"'>]+)", text or ""):
            found.append(match.group(1))
        for match in PATH_RE.finditer(re.sub(r"https?://\S+", " ", text or "")):
            found.append(match.group(1))
    unique = []
    for path in found:
        if ".." in path:
            continue  # never step outside the repo, even through the API
        path = path.removeprefix("./").lstrip("/")
        if path and path not in unique:
            unique.append(path)
    return unique[:MAX_FILES]


def github_context(owner, repo, number):
    gh = GitHubReader(owner, repo)
    issue = gh.get(f"/issues/{number}")
    comments = gh.get(f"/issues/{number}/comments", params={"per_page": LIMITS["comments"]}) or []
    readme = gh.get("/readme", raw=True, optional=True) or ""
    contributing = ""
    for path in CONTRIBUTING_PATHS:
        contributing = gh.get(f"/contents/{quote(path)}", raw=True, optional=True) or ""
        if contributing:
            break
    languages = gh.get("/languages", optional=True) or {}
    timeline = gh.get(f"/issues/{number}/timeline", params={"per_page": 100}, optional=True) or []
    comment_texts = [c.get("body") or "" for c in comments]
    files = {}
    for path in mentioned_paths(issue.get("body") or "", *comment_texts):
        text = gh.get(f"/contents/{quote(path)}", raw=True, optional=True)
        if isinstance(text, str) and text:
            files[path] = text[:LIMITS["file"]]
    open_prs = []
    for event in timeline if isinstance(timeline, list) else []:
        source_issue = ((event or {}).get("source") or {}).get("issue") or {}
        if event.get("event") == "cross-referenced" and "pull_request" in source_issue \
                and source_issue.get("state") == "open":
            open_prs.append({"number": source_issue.get("number"), "title": source_issue.get("title") or "",
                             "url": source_issue.get("html_url") or ""})
    return {
        "issue": issue, "comments": comments, "readme": readme[:LIMITS["readme"]],
        "contributing": contributing[:LIMITS["contributing"]], "languages": languages, "files": files,
        "open_prs": open_prs,
    }


def claimability(issue, open_prs):
    """Warnings worked out from the API data (not the AI)."""
    warnings = []
    if issue.get("state") == "closed":
        warnings.append("This issue is closed.")
    assignees = [a.get("login") for a in issue.get("assignees") or [] if a.get("login")]
    if assignees:
        warnings.append(f"Already assigned to {', '.join('@' + a for a in assignees)} - ask before starting.")
    for pr in open_prs:
        warnings.append(f"Open pull request #{pr['number']} already references this issue: {pr['title']}".strip())
    if issue.get("locked"):
        warnings.append("The issue is locked - comments are limited to collaborators.")
    return warnings


def _context_text(opp, ctx):
    issue = ctx["issue"]
    langs = ctx["languages"]
    total = sum(langs.values()) or 1
    lines = [
        f"ISSUE #{issue.get('number')}: {issue.get('title')}",
        f"State: {issue.get('state')} | Labels: {', '.join(l.get('name', '') for l in issue.get('labels') or [])}",
        f"Assignees: {', '.join(a.get('login', '') for a in issue.get('assignees') or []) or 'none'}",
        f"Open PRs referencing it: {', '.join('#' + str(p['number']) for p in ctx['open_prs']) or 'none'}",
        "", (issue.get("body") or "")[:LIMITS["issue"]], "", "COMMENTS:",
    ]
    for c in ctx["comments"][:LIMITS["comments"]]:
        lines.append(f"- @{(c.get('user') or {}).get('login', '?')}: {(c.get('body') or '')[:LIMITS['comment']]}")
    lines += ["", "LANGUAGES: " + ", ".join(f"{k} {v * 100 // total}%" for k, v in sorted(langs.items(), key=lambda kv: -kv[1])),
              "", "README:", ctx["readme"], "", "CONTRIBUTING:", ctx["contributing"] or "(none found)"]
    for path, text in ctx["files"].items():
        lines += ["", f"FILE {path} (excerpt, read-only):", text]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

COMMON_RULES = """Use ONLY the information given. Never invent facts, payout amounts, requirements, or file names
that aren't supported by the text. Be honest about difficulty and time - say "unclear" when it is.
Return ONLY a JSON object."""

GITHUB_PROMPT = COMMON_RULES + """ You plan how a developer would complete a GitHub bounty issue.
{
 "summary": what is being asked, in 2-4 sentences,
 "claimable": your reading of whether it's still open to work on and how to claim it (e.g. comment first),
 "setup_steps": [local setup steps from the README/CONTRIBUTING],
 "files_to_change": [{"path": "...", "why": "..."}] (likely files, only from the given text/files),
 "testing": [how to test the change],
 "submit_pr": [how to submit the PR per CONTRIBUTING],
 "payout_quote": EXACT quote from the issue or comments stating payout terms, or "",
 "difficulty": "easy" | "medium" | "hard" | "unclear",
 "time_estimate_hours": number or null,
 "time_estimate_note": short reason,
 "questions": [questions to ask the maintainer],
 "steps": [{"text": one checklist step, "manual": true}]
}"""

INCOME_PROMPT = COMMON_RULES + """ You help someone apply for a job, freelance project, or paid task.
{
 "summary": what the opportunity is, in 2-3 sentences,
 "application_steps": [how to apply, in order],
 "prepare": [what to prepare before applying],
 "red_flags": [anything in the text that looks risky or unclear],
 "questions": [questions to ask the client/employer],
 "difficulty": "easy" | "medium" | "hard" | "unclear",
 "time_estimate_hours": number or null (to apply),
 "time_estimate_note": short reason,
 "steps": [{"text": one checklist step, "manual": true}]
}"""

CRYPTO_PROMPT = COMMON_RULES + """ You explain the steps of a crypto opportunity. Never suggest sharing a seed
phrase or private key, and never suggest connecting a wallet to an unverified site.
{
 "summary": what the opportunity is, in 2-3 sentences,
 "steps": [{"text": one step, "needs_wallet": true if it involves a wallet, signing, approving, or a transaction}],
 "costs": stated costs, fees or deposits, or "none stated",
 "questions": [questions worth answering before starting],
 "difficulty": "easy" | "medium" | "hard" | "unclear",
 "time_estimate_hours": number or null,
 "time_estimate_note": short reason
}"""


def _strings(value, limit=20, length=400):
    return [str(v).strip()[:length] for v in (value if isinstance(value, list) else []) if str(v).strip()][:limit]


def _steps(value):
    steps = []
    for step in value if isinstance(value, list) else []:
        text = str(step.get("text") if isinstance(step, dict) else step or "").strip()
        if text:
            steps.append({"text": text[:450], "manual": True,
                          "needs_wallet": bool(isinstance(step, dict) and step.get("needs_wallet"))})
    return steps[:MAX_STEPS]


def _estimate(data):
    difficulty = data.get("difficulty") if data.get("difficulty") in ("easy", "medium", "hard", "unclear") else "unclear"
    try:
        hours = round(float(data.get("time_estimate_hours")), 1)
        hours = hours if hours > 0 else None
    except (TypeError, ValueError):
        hours = None
    return difficulty, hours, str(data.get("time_estimate_note") or "")[:300]


def kind_for(opp):
    if github_issue_ref(opp.url):
        return "github"
    return "crypto" if opp.opportunity_type == "crypto" else "income"


# ---------------------------------------------------------------------------
# Generate
# ---------------------------------------------------------------------------

def generate(opp, client=None):
    """Builds and saves a new Walkthrough for the opportunity."""
    kind = kind_for(opp)
    warnings = list(opp.safety_reasons or [])
    sections = []
    payout_quote = ""
    if kind == "github":
        owner, repo, number = github_issue_ref(opp.url)
        try:
            ctx = github_context(owner, repo, number)
        except requests.RequestException as exc:
            raise WalkthroughError("Couldn't reach GitHub right now.") from exc
        warnings = claimability(ctx["issue"], ctx["open_prs"]) + warnings
        data, cost = run_json(GITHUB_PROMPT, _context_text(opp, ctx), "walkthrough", opportunity=opp, client=client,
                              max_tokens=4000)
        issue_text = "\n".join([ctx["issue"].get("title") or "", ctx["issue"].get("body") or ""]
                               + [c.get("body") or "" for c in ctx["comments"]])
        quote_text = str(data.get("payout_quote") or "").strip()
        payout_quote = quote_text if quote_text and quote_in_text(quote_text, issue_text) else ""
        files = [f"{f.get('path')}: {f.get('why', '')}".strip(": ") for f in data.get("files_to_change") or []
                 if isinstance(f, dict) and f.get("path")][:15]
        sections = [
            ("What's being asked", _strings([data.get("summary")], 1, 1500)),
            ("Still open / claimable?", _strings([data.get("claimable")], 1, 800)),
            ("Local setup", _strings(data.get("setup_steps"))),
            ("Likely files to change", files),
            ("How to test", _strings(data.get("testing"))),
            ("How to submit the PR", _strings(data.get("submit_pr"))),
            ("Repository", [f"{owner}/{repo} - languages: " + (", ".join(ctx["languages"]) or "unknown"),
                            "Files read (text only, never run): " + (", ".join(ctx["files"]) or "none mentioned")]),
        ]
        steps = _steps(data.get("steps"))
    elif kind == "crypto":
        text = f"TITLE: {opp.title}\nURL: {opp.url}\nSTATED COSTS: ${opp.costs_usd}\n\n{opp.raw_text}"
        data, cost = run_json(CRYPTO_PROMPT, text, "walkthrough", opportunity=opp, client=client)
        steps = _steps(data.get("steps"))
        for step in steps:   # anything touching a wallet is always done by hand
            if step["needs_wallet"] or WALLET_RE.search(step["text"]):
                step["needs_wallet"] = True
                step["manual"] = True
        costs = [f"Stated costs: ${opp.costs_usd:.2f}" + (" (requires spending)" if opp.requires_spending else "")]
        if opp.refundable_deposit_usd:
            costs.append(f"Refundable deposit: ${opp.refundable_deposit_usd:.2f}")
        sections = [("Summary", _strings([data.get("summary")], 1, 1500)),
                    ("Costs", costs + _strings([data.get("costs")], 1)),
                    ("Wallet steps (always manual - this app never connects a wallet or signs anything)",
                     [s["text"] for s in steps if s["needs_wallet"]] or ["none"])]
    else:
        text = f"TITLE: {opp.title}\nURL: {opp.url}\n\n{opp.raw_text}"
        data, cost = run_json(INCOME_PROMPT, text, "walkthrough", opportunity=opp, client=client)
        sections = [("Summary", _strings([data.get("summary")], 1, 1500)),
                    ("How to apply", _strings(data.get("application_steps"))),
                    ("What to prepare", _strings(data.get("prepare"))),
                    ("Red flags noticed", _strings(data.get("red_flags")) or ["none noticed - still check the safety notes"])]
        steps = _steps(data.get("steps"))

    difficulty, hours, note = _estimate(data)
    content = {
        "kind": kind,
        "warnings": warnings,
        "sections": [{"title": title, "items": items} for title, items in sections if items],
        "payout_quote": payout_quote,
        "difficulty": difficulty, "time_estimate_hours": hours, "time_estimate_note": note,
        "questions": _strings(data.get("questions")),
        "steps": steps,
    }
    from . import config
    return Walkthrough.objects.create(opportunity=opp, kind=kind, content=content, cost=cost,
                                      model_name=config.ai_review_cfg().get("model", ""))


def add_steps_to_checklist(walkthrough):
    """The walkthrough's steps become TaskSteps (after any existing ones,
    skipping exact duplicates). Wallet steps are labelled manual; none is
    marked automatable."""
    opp = walkthrough.opportunity
    existing = set(opp.task_steps.values_list("text", flat=True))
    order = (opp.task_steps.order_by("-order").values_list("order", flat=True).first() or 0) + 1
    created = []
    for step in walkthrough.content.get("steps") or []:
        text = (("Manual (wallet): " if step.get("needs_wallet") else "") + step["text"])[:500]
        if text in existing:
            continue
        created.append(TaskStep(opportunity=opp, text=text, order=order, automatable=False))
        existing.add(text)
        order += 1
    TaskStep.objects.bulk_create(created)
    return created
