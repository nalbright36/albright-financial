"""Food > "Log food with your AI": the instructions to paste into the person's AI (custom instructions / project),
made with this site's address (albright_bot_api.food_ai), and how to give it a key. The key itself is never on this
page - it goes in the AI's secret / connection settings."""
from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.urls import reverse


@login_required
def connect(request):
    from albright_bot_api import food_ai, scopes
    base = food_ai.api_base(request)
    return render(request, "albright_fitness/food_ai.html", {
        "instructions": food_ai.instructions(base), "openapi_url": base + "/fitness/openapi.json",
        "keys_allowed": scopes.keys_allowed(request.user), "keys_url": reverse("albright_bot_api:my_keys")})
