from cryptography.fernet import Fernet, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

GENERATE = '`python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`'


def _fernet(key, name):
    if not key:
        raise ImproperlyConfigured(f"{name} is not set. Generate one with {GENERATE} and put it in .env.")
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except (ValueError, TypeError):
        raise ImproperlyConfigured(f"{name} isn't a valid Fernet key. Generate one with {GENERATE}.") from None


def primary_fernet():
    """Just the current key (ALPACA_CREDENTIALS_ENCRYPTION_KEY) - what new
    values are encrypted with, and what rotate_encryption_key checks against."""
    return _fernet(getattr(settings, "ALPACA_CREDENTIALS_ENCRYPTION_KEY", None), "ALPACA_CREDENTIALS_ENCRYPTION_KEY")


def old_fernets():
    """The previous key(s) (ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS) - set only
    while a key rotation is in progress."""
    return [_fernet(key, "ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS")
            for key in getattr(settings, "ALPACA_CREDENTIALS_ENCRYPTION_OLD_KEYS", None) or []]


def get_fernet():
    """
    The cipher for every value stored encrypted (Alpaca credentials, the
    Gmail token): a MultiFernet that ENCRYPTS with the current key and
    DECRYPTS with the current key or any old one, so stored values stay
    readable while a key rotation is in progress (see the
    rotate_encryption_key management command).

    The key is deliberately separate from Django's SECRET_KEY - they have
    different rotation and exposure concerns. Both live in .env only. If the
    key is ever lost, every stored credential becomes permanently
    undecryptable - back it up somewhere safe (e.g. a password manager).
    """
    return MultiFernet([primary_fernet(), *old_fernets()])
