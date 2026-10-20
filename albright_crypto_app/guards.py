"""Security guards for the crypto app.

reject_secrets(): text a person types (notes, pasted opportunity text) is
refused if it contains what looks like a wallet secret - this app must
never store one. Deliberately narrow so ordinary text never trips it:
  - a PEM private key block
  - a WIF (Bitcoin wallet import format) private key
  - a 64-hex value labeled as a private key/secret
  - 12+ words labeled as a seed/recovery/mnemonic phrase
(A bare 64-hex string is NOT rejected on its own - transaction hashes look
exactly like that and are public.)
"""
import re

from django.core.exceptions import ValidationError

SECRET_PATTERNS = {
    "a private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "a WIF private key": re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[5KL][1-9A-HJ-NP-Za-km-z]{50,51}(?![1-9A-HJ-NP-Za-km-z])"),
    "a labeled private key": re.compile(
        r"(private[\s_-]*key|secret[\s_-]*key|priv[\s_-]*key)\s*[:=]?\s*(0x)?[0-9a-fA-F]{64}\b", re.I),
    "a seed/recovery phrase": re.compile(
        r"(seed|recovery|mnemonic|secret)[\s_-]*(phrase|words?)?\s*[:=]\s*([a-z]{3,8}[\s,]+){11,}[a-z]{3,8}", re.I),
}

SECRET_MESSAGE = ("This looks like {what}. Never paste wallet secrets anywhere - this app doesn't store them, "
                  "and no legitimate opportunity needs them.")


def find_secret(text):
    """The kind of secret found in text, or "" if none."""
    for what, pattern in SECRET_PATTERNS.items():
        if text and pattern.search(text):
            return what
    return ""


def reject_secrets(text, field=None):
    what = find_secret(text)
    if what:
        message = SECRET_MESSAGE.format(what=what)
        raise ValidationError({field: message} if field else message)
