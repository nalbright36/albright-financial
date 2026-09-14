# albright_trading_app/context_processors.py  (or a shared/utils location)
def current_app(request):
    resolved = request.resolver_match
    app_name = resolved.app_name if resolved else None
    return {"in_reselling_app": app_name == "albright_reselling_app"}   