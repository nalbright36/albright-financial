"""Seeds clearly-marked PLACEHOLDER content so the page has a shape to fill in.

Everything is created HIDDEN (published=False) and the page and chat start
switched OFF, so nothing placeholder can go public by accident. Lines that
contain [PLACEHOLDER] are also stripped from what the chatbot reads.
Project blurbs are drafts written from the repository's code, not from
Nathan; rewrite them in your own words. No job history, dates, employers or
education are invented: those rows are empty templates."""
from django.db import migrations

KNOWLEDGE_TEMPLATE = """# About Nathan (knowledge document for the resume chatbot)
# The chatbot answers ONLY from this document plus the published resume sections.
# Everything here should be safe to show the public. Lines containing [PLACEHOLDER] are ignored by the bot.

## Who I am
[PLACEHOLDER] Two or three sentences: what you do, what you're good at, what you want next.

## What I'm looking for
[PLACEHOLDER] Role types, remote/hybrid/on-site preference, earliest start date, work authorization if you want to share it.

## Work history (detail beyond the resume)
[PLACEHOLDER] For each role: what the team did, what you owned, a result you're proud of.

## Projects (the story behind them)
[PLACEHOLDER] Why you built the site, the hardest problem you solved, what you'd do differently.

## Skills and tools
[PLACEHOLDER] Languages, frameworks, databases, cloud/hosting, AI APIs, and how long you've used each.

## Education and certifications
[PLACEHOLDER] Degrees, courses, certifications, with years.

## Working style
[PLACEHOLDER] How you like to work, collaborate, learn, and handle feedback.

## Frequently asked by recruiters
[PLACEHOLDER] Q: Are you open to relocation? A: ...
[PLACEHOLDER] Q: When can you start? A: ...
"""

PROJECTS = [
    {
        "order": 10, "title": "Albright Financial (this website)",
        "tagline": "[PLACEHOLDER] One-line pitch",
        "description": "[PLACEHOLDER — review and rewrite in your own words] A Django 5.2 web platform hosted on "
                       "PythonAnywhere with a Postgres database, bringing several tools together behind one login: "
                       "an algorithmic trading app, a reselling/auction toolkit, an opportunities inbox, and a "
                       "superuser-only admin area with system health, integrations, AI usage and audit logging.",
        "tech": "Python, Django, PostgreSQL, PythonAnywhere, JavaScript, HTML/CSS",
    },
    {
        "order": 20, "title": "Bot API",
        "tagline": "[PLACEHOLDER] One-line pitch",
        "description": "[PLACEHOLDER — review and rewrite] A token-authenticated JSON API that lets an AI assistant "
                       "work with the site safely: scoped bearer tokens that can be revoked or rotated, HTTPS "
                       "enforcement, per-request activity logging, and a suggestions inbox for ideas sent by the bot.",
        "tech": "Python, Django, REST/JSON, token auth",
    },
    {
        "order": 30, "title": "Flip calculator & auction scrapers",
        "tagline": "[PLACEHOLDER] One-line pitch",
        "description": "[PLACEHOLDER — review and rewrite] Tools for finding profitable resale items: scrapers for "
                       "online auction sites (HiBid, MaxSold, ShopGoodwill), a flip/max-bid calculator using fees and "
                       "eBay comparables, coin and jewelry valuation helpers, AI-assisted lot review, and bid "
                       "watching with alerts.",
        "tech": "Python, Playwright, requests, eBay API, Anthropic API",
    },
    {
        "order": 40, "title": "Options trading engine",
        "tagline": "[PLACEHOLDER] One-line pitch",
        "description": "[PLACEHOLDER — review and rewrite] A strategy engine on the Alpaca API that runs configurable "
                       "stock and options strategies: picks contracts from the option chain by target days-to-"
                       "expiration, tracks positions, and enforces safety rails such as a forced close before "
                       "expiration. Also uses news and Reddit sentiment signals.",
        "tech": "Python, Django, Alpaca API, pandas",
    },
]


def seed(apps, schema_editor):
    ResumeSettings = apps.get_model("albright_resume", "ResumeSettings")
    Project = apps.get_model("albright_resume", "Project")
    WorkExperience = apps.get_model("albright_resume", "WorkExperience")
    EducationItem = apps.get_model("albright_resume", "EducationItem")
    if not ResumeSettings.objects.filter(pk=1).exists():
        ResumeSettings.objects.create(
            pk=1, page_enabled=False, chat_enabled=False,
            headline="[PLACEHOLDER] e.g. Software Developer · Python & Django",
            open_to="[PLACEHOLDER] e.g. Open to full-time developer roles",
            summary="[PLACEHOLDER] Write 3-4 sentences about yourself, or use Admin > Resume > AI summary to draft one.",
            knowledge_doc=KNOWLEDGE_TEMPLATE,
        )
    if not Project.objects.exists():
        for p in PROJECTS:
            Project.objects.create(published=False, show_in_built=True,
                                   details_for_ai="[PLACEHOLDER] The full story: problem, approach, stack, results.", **p)
    if not WorkExperience.objects.exists():
        WorkExperience.objects.create(published=False, role="[PLACEHOLDER] Job title", organization="[PLACEHOLDER] Employer",
                                      highlights="[PLACEHOLDER] One achievement per line")
    if not EducationItem.objects.exists():
        EducationItem.objects.create(published=False, title="[PLACEHOLDER] Degree or certification",
                                     institution="[PLACEHOLDER] School or issuer")


class Migration(migrations.Migration):
    dependencies = [("albright_resume", "0001_initial")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
