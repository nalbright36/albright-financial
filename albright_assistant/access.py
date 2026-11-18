"""Who may use the AI assistant, and the encrypted key.

  - Permission: "Can use AI assistant" (Admin > Users; UserPreference.assistant_allowed, default off). Superusers
    always may. Without it there's no button, no setup page (404) and the assistant's API calls are refused
    (albright_bot_api.auth.authenticate's in-process path).
  - Admin > AI assistant "Disable": the person's connection stops working (Connection.disabled) until re-enabled.
  - The key: encrypted with the site's MultiFernet (crypto_utils.get_fernet - the same as the Alpaca credentials),
    decrypted only in this process to call the provider. Only the last 4 characters are ever shown. It's never
    logged, never sent to the browser, never returned by a view.
"""
from albright_trading_app.crypto_utils import get_fernet


def allowed(user):
    if not (user and getattr(user, "is_authenticated", False) and user.is_active):
        return False
    if user.is_superuser:
        return True
    from albright_labs.models import UserPreference
    pref = UserPreference.objects.filter(user=user).only("assistant_allowed").first()
    return bool(pref and pref.assistant_allowed)


def connection(user):
    """The person's connection, or None."""
    from .models import Connection
    return Connection.objects.filter(user=user).first() if user and user.is_authenticated else None


def encrypt(raw):
    return get_fernet().encrypt(raw.encode()).decode()


def decrypt(stored):
    return get_fernet().decrypt(stored.encode()).decode()


def last4(raw):
    return raw[-4:] if len(raw) >= 4 else raw


def assistant_token(user, refresh=True):
    """The in-process API token the assistant acts with: one per person (BotToken kind "assistant", named "AI
    assistant" - what the activity feed shows), its scopes refreshed to everything the person can use right now
    (admin-only scopes only for superusers - albright_bot_api.scopes.available). Its value is thrown away: it can't
    be used over HTTP."""
    from albright_bot_api import scopes as scope_rules
    from albright_bot_api.models import ASSISTANT, BotToken
    from .models import Connection
    conn = Connection.objects.filter(user=user).select_related("token").first()
    token = conn.token if conn and conn.token_id else None
    if token is None or token.revoked_at is not None:
        token, _raw = BotToken.issue(user, "AI assistant", [])
        BotToken.all_objects.filter(pk=token.pk).update(kind=ASSISTANT)
        token.kind = ASSISTANT
        if conn:
            conn.token = token
            conn.save(update_fields=["token"])
    if refresh:
        wanted = scope_rules.available(user)
        if list(token.scopes) != wanted:
            token.scopes = wanted
            BotToken.all_objects.filter(pk=token.pk).update(scopes=wanted)
    return token
