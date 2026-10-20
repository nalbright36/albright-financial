"""Scam and risk checks for crypto opportunities. Pure Python, no Django.

check_opportunity() returns a verdict:
  "block" - never show as actionable (asks for keys, deposits to unlock, lookalike domain...)
  "warn"  - show, but with the warnings visible
  "ok"    - no red flags found (NOT a guarantee of safety)
"""
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

# Anything here = automatic block. These are the patterns behind most crypto theft.
BLOCK_PATTERNS = {
    "asks for seed/recovery phrase": r"seed\s*phrase|recovery\s*phrase|secret\s*recovery|mnemonic|12[- ]word|24[- ]word",
    "asks for private key": r"private\s*key|export\s*(your\s*)?key",
    # Sending funds to receive funds, or paying to "unlock"/withdraw earnings, is the classic task scam.
    # (Paying ordinary gas to claim is a warning, not a block - see WARN_PATTERNS.)
    "deposit or payment required to unlock": (r"(deposit|send)\b.{0,40}\b(to|in order to)\s*(unlock|receive|claim|withdraw|activate)"
                                              r"|pay\b.{0,40}\bto\s*(unlock|activate|withdraw)"),
    "promises guaranteed or multiplied returns": r"guaranteed\s*(returns?|profit|income)|double\s*your|(2|3|10)x\s*your\s*(crypto|money|coins)|risk[- ]free\s*(profit|returns?)",
    "wallet 'validation' or 'sync' request": r"(validate|verify|sync|rectify|restore)\s*(your\s*)?wallet",
    "support contact through DMs": r"(dm|direct message|whatsapp|telegram)\s*(us|me|support|admin)\s*(to|for)\s*(claim|support|help)",
}
# These raise the risk score but don't block on their own.
WARN_PATTERNS = {
    "requires connecting a wallet": r"connect\s*(your\s*)?wallet",
    "requires token approval or signing": r"approve\s*(the\s*)?(token|spending|contract)|sign\s*(a\s*)?(message|transaction)",
    "urgency pressure": r"(expires?|ends?)\s*in\s*\d+\s*(minutes?|hours?)|act\s*now|last\s*chance|limited\s*spots",
    "claim fee or gas required": r"claim\s*fee|gas\s*fee|pay\s*gas|network\s*fee",
    "requires purchase or deposit": r"(minimum|min\.?)\s*deposit|purchase\s*required|buy\s*\$?\d+",
    "giveaway language": r"giveaway|free\s*\d+\s*(eth|btc|usdt|sol)",
    "requires identity documents": r"\bkyc\b|passport|driver'?s\s*licen[cs]e|selfie\s*with",
}


@dataclass
class SafetyResult:
    verdict: str = "ok"                      # ok / warn / block
    risk_score: int = 0                      # 0-100
    reasons: list = field(default_factory=list)
    domain: str = ""
    domain_status: str = "unknown"           # allowlisted / lookalike / unknown / invalid


def _domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def check_domain(url: str, allowlist: list) -> tuple[str, str]:
    """Return (domain, status). Allowlisted means the exact domain or a subdomain of an allowlisted one."""
    domain = _domain(url)
    if not domain:
        return "", "invalid"
    allow = [d.lower().removeprefix("www.") for d in allowlist]
    if any(domain == d or domain.endswith("." + d) for d in allow):
        return domain, "allowlisted"
    if "xn--" in domain:
        return domain, "lookalike"           # punycode: classic impersonation trick
    name = domain.rsplit(".", 1)[0].split(".")[-1]
    for d in allow:
        dname = d.rsplit(".", 1)[0].split(".")[-1]
        if name != dname and len(dname) >= 4 and (_edit_distance(name, dname) <= 2 or dname in name):
            return domain, "lookalike"       # e.g. "galxe-claim", "githhub", "coinbasse"
    return domain, "unknown"


def check_opportunity(text: str, url: str, allowlist: list) -> SafetyResult:
    t = (text or "").lower()
    res = SafetyResult()
    res.domain, res.domain_status = check_domain(url, allowlist)

    for reason, pat in BLOCK_PATTERNS.items():
        if re.search(pat, t):
            res.reasons.append(reason)
            res.verdict = "block"
    if res.domain_status == "lookalike":
        res.reasons.append(f"domain looks like an impersonation of a trusted site ({res.domain})")
        res.verdict = "block"
    if res.domain_status == "invalid":
        res.reasons.append("missing or invalid link")
        res.verdict = "block"

    score = 100 if res.verdict == "block" else 0
    if res.verdict != "block":
        for reason, pat in WARN_PATTERNS.items():
            if re.search(pat, t):
                res.reasons.append(reason)
                score += 20
        if res.domain_status == "unknown":
            res.reasons.append("source domain not on your verified list")
            score += 25
        res.verdict = "warn" if score else "ok"
    res.risk_score = min(score, 100)
    return res