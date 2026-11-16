"""Food > "Log food with your AI": the same instructions block as My account > API keys (albright_bot_api.connect -
made with this site's address, food logging included), and a link to the step-by-step "Connect your AI" directions
there. A key is never on this page."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse


@login_required
def connect(request):
    from albright_bot_api import connect as ai, scopes
    links = ai.urls(request)
    return render(request, "albright_fitness/food_ai.html", {
        "instructions": ai.instructions(links["base"]), "openapi_url": links["openapi"],
        "keys_allowed": scopes.keys_allowed(request.user), "keys_url": reverse("labs:api_keys") + "#connect"})
