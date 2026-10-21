"""skill_match: the share (0-100) of an income opportunity's required skills
that are in my skills profile (Settings page).

Skills are compared loosely: case, spaces and punctuation are ignored
("Power BI" = "powerbi", "API integrations" = "API integration") and a few
common aliases map to one name ("Postgres" = "PostgreSQL"). Duplicate
requirements count once. No required skills listed -> None (unknown), not 0.
"""
import re

ALIASES = {
    "postgres": "postgresql", "psql": "postgresql",
    "msexcel": "excel", "microsoftexcel": "excel",
    "microsoftpowerbi": "powerbi",
    "py": "python", "python3": "python",
    "webscrape": "webscraping", "scraping": "webscraping",
    "api": "apiintegration", "restapi": "apiintegration",
}


def normalize(skill):
    key = re.sub(r"[^a-z0-9+#]", "", str(skill or "").lower())
    if key in ALIASES:
        return ALIASES[key]  # before the plural rule: "postgres" isn't a plural
    if len(key) > 3 and key.endswith("s") and not key.endswith("ss"):
        key = key[:-1]  # "integrations" -> "integration" (applied to both sides)
    return ALIASES.get(key, key)


def skill_match(required, mine):
    required_keys = {normalize(s) for s in required or [] if normalize(s)}
    if not required_keys:
        return None
    my_keys = {normalize(s) for s in mine or []}
    return round(100 * len(required_keys & my_keys) / len(required_keys))


def matched_skills(required, mine):
    """The required skills (as written) that I have - for the detail page."""
    my_keys = {normalize(s) for s in mine or []}
    return [s for s in required or [] if normalize(s) in my_keys]
