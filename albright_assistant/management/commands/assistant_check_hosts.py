"""python manage.py assistant_check_hosts - can this server reach each AI provider's API over HTTPS?

Run it on the server (PythonAnywhere: a Bash console). Each host gets one request WITHOUT a key - a 401/403 answer
means the host is reachable (it's the provider saying "no key"); a connection error, proxy 403 or timeout means it
isn't. Uses the environment's proxy settings, like the assistant does (requests honours http(s)_proxy)."""
import requests
from django.core.management.base import BaseCommand

from albright_assistant import providers

PROBES = {"openai": "https://api.openai.com/v1/models", "anthropic": "https://api.anthropic.com/v1/models",
          "xai": "https://api.x.ai/v1/models", "gemini": "https://generativelanguage.googleapis.com/v1beta/models",
          "openrouter": "https://openrouter.ai/api/v1/key"}


class Command(BaseCommand):
    help = "Checks outbound HTTPS to each AI provider's API host (no key sent)."

    def handle(self, *args, **options):
        failed = 0
        for provider in providers.PROVIDERS:
            url = PROBES[provider.key]
            try:
                response = requests.get(url, timeout=15, headers={"User-Agent": providers.USER_AGENT})
            except requests.RequestException as exc:
                failed += 1
                self.stdout.write(self.style.ERROR(f"FAIL  {provider.label:<20} {provider.host}: {type(exc).__name__}"))
                continue
            reachable = response.status_code < 500 and "pythonanywhere" not in response.text[:500].lower()
            line = f"{'OK  ' if reachable else 'FAIL'}  {provider.label:<20} {provider.host}: HTTP {response.status_code}"
            if not reachable:
                failed += 1
            self.stdout.write(self.style.SUCCESS(line) if reachable else self.style.ERROR(line))
        if failed:
            self.stdout.write(self.style.WARNING(f"{failed} host(s) unreachable."))
