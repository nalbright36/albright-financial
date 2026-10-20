# albright_trading_app/context_processors.py  (or a shared/utils location)
def current_app(request):
    """Which of the three apps the current page belongs to - drives the
    header's app-switch buttons, per-app nav, and accent (templates/index.html)."""
    resolved = request.resolver_match
    app_name = resolved.app_name if resolved else None
    return {
        "in_reselling_app": app_name == "albright_reselling_app",
        "in_crypto_app": app_name == "albright_crypto_app",
    }
