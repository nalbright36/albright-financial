"""assistant: what templates/index.html needs for the floating assistant button - only for people with "Can use AI
assistant" (none for anyone else, so no button). Two small queries per page, signed-in pages only."""
from . import access, prompt, providers

# Starter chips per app (the open app's, else Home's). "item" chips only on a page showing that kind of item.
CHIPS = {
    "fitness": ["Log what I ate", "How did my week go?"],
    "planner": ["What's on today?", "Add a task for tomorrow"],
    "meals": ["Plan dinners this week", "What's on the grocery list?"],
    "notes": ["Find my notes about…"],
    "home": ["What's on today?"],
}
ITEM_CHIPS = {"note": "Summarize this note", "recipe": "Add this recipe to groceries", "task": "Move this to tomorrow",
              "workout": "Summarize this workout"}


def assistant(request):
    user = getattr(request, "user", None)
    if not (user and user.is_authenticated) or not access.allowed(user):
        return {}
    conn = access.connection(user)
    page = prompt.page_context(request)
    app = getattr(getattr(request, "resolver_match", None), "namespace", "") or ""
    from albright_labs import apps_registry
    found = apps_registry.app_for_namespace(app) if app else None
    key = found.key if found else "home"
    chips = list(CHIPS.get(key, CHIPS["home"]))
    item = page.get("item")
    if item and item["type"] in ITEM_CHIPS:
        chips.insert(0, ITEM_CHIPS[item["type"]])
    return {"assistant_widget": {
        "connected": bool(conn and not conn.disabled and conn.model),
        "disabled": bool(conn and conn.disabled),
        "provider": conn.provider_label if conn else "", "model": conn.model_label if conn else "",
        "page": page, "chips": chips[:3],
        # Photos: only for a model that reads images (providers.supports_images) - else the button is disabled.
        "vision": bool(conn and providers.supports_images(conn.provider, conn.model, conn.models_cache)),
    }}
