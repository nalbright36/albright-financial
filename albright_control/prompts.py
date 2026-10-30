"""'Copy as Claude Code prompt' for a Bot suggestion."""

APP_CONTEXT = {
    "trade": "The Trading app (albright_trading_app).",
    "resell": "The Reselling app (albright_reselling_app).",
    "opps": "The Opportunities app (albright_crypto_app).",
    "all": "All three apps (Trading, Reselling, Opportunities) - shared templates/styles where possible.",
}
CLOSING = ("Keep the existing patterns, styling, and vanilla JS.\n\n"
           "Run python manage.py check and python manage.py test --keepdb. Before finishing, run makemigrations "
           "and migrate for every app you changed, and confirm showmigrations shows nothing unapplied.")


def claude_code_prompt(suggestion):
    lines = [suggestion.title.strip(), "", f"App: {APP_CONTEXT[suggestion.app]}"]
    if suggestion.area:
        lines.append(f"Area: {suggestion.area.strip()}")
    lines.append(f"Priority: {suggestion.get_priority_display().lower()}")
    lines += ["", suggestion.description.strip()]
    if suggestion.my_notes.strip():
        lines += ["", "My notes:", suggestion.my_notes.strip()]
    lines += ["", CLOSING]
    return "\n".join(lines)
