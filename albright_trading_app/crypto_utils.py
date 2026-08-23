from cryptography.fernet import Fernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def get_fernet():
    """
    Returns a Fernet instance built from ALPACA_CREDENTIALS_ENCRYPTION_KEY.

    This key is deliberately separate from Django's SECRET_KEY — mixing
    encryption keys with the secret Django uses for sessions/CSRF/etc.
    is bad practice, since the two have different rotation and exposure
    concerns. Generate one once with:

        python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

    and store the output as an environment variable. Never commit it to
    source control. If this key is ever lost, every stored Alpaca
    credential becomes permanently undecryptable — treat it like a
    password, back it up somewhere safe (e.g. a password manager).
    """
    key = getattr(settings, "ALPACA_CREDENTIALS_ENCRYPTION_KEY", None)
    if not key:
        raise ImproperlyConfigured(
            "ALPACA_CREDENTIALS_ENCRYPTION_KEY is not set. Generate one with "
            '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` '
            "and set it as an environment variable."
        )
    if isinstance(key, str):
        key = key.encode()
    return Fernet(key)