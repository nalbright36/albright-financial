"""'Copy as Claude Code prompt' for a Bot suggestion."""

APP_CONTEXT = {
    "trade": "The Trading app (albright_trading_app).",
    "resell": "The Reselling app (albright_reselling_app).",
    "opps": "The Opportunities app (albright_crypto_app).",
    "planner": "The Planner app (albright_planner).",
    "meals": "The Meals app (albright_meals).",
    "money": "The Money app (albright_money).",
    "fitness": "The Fitness app (albright_fitness).",
    "ideas": "Admin > Ideas (albright_control, /control/ideas/).",
    "admin": "The Admin area (albright_control, /control/).",
    "all": "All three apps (Trading, Reselling, Opportunities) - shared templates/styles where possible.",
}
CLOSING = ("Keep the existing patterns, styling, and vanilla JS.\n\n"
           "Run python manage.py check and python manage.py test --keepdb. Before finishing, run makemigrations "
           "and migrate for every app you changed, and confirm showmigrations shows nothing unapplied.")


def app_context(slug):
    """The "App:" line - never a KeyError: a slug without a description (a new app the bot can name, or an old
    value) still reads sensibly, and an archived app says so (albright_labs.archive)."""
    from albright_labs.archive import is_app_archived
    text = APP_CONTEXT.get(slug) or (f"The {str(slug).title()} app." if slug else APP_CONTEXT["all"])
    if slug and slug != "all" and is_app_archived(slug):
        text += " (This app is archived.)"
    return text


def claude_code_prompt(suggestion):
    lines = [(suggestion.title or "").strip(), "", f"App: {app_context(suggestion.app)}"]
    if suggestion.area:
        lines.append(f"Area: {suggestion.area.strip()}")
    lines.append(f"Priority: {suggestion.get_priority_display().lower()}")
    lines += ["", (suggestion.description or "").strip()]
    if (suggestion.my_notes or "").strip():
        lines += ["", "My notes:", suggestion.my_notes.strip()]
    lines += ["", CLOSING]
    return "\n".join(lines)
