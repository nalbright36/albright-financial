"""The crypto app's security rules, enforced as tests:

  1. No model, form, or setting can store a seed phrase, private key, or
     exchange API key - checked by field/setting names - and typed-in text
     containing one is refused.
  2. Nothing in the app signs or sends transactions, connects wallets,
     approves tokens, buys anything, or posts to outside sites - checked
     by scanning the app's own source (Python, templates, its JS).
"""
import re
from pathlib import Path

from django import forms as django_forms
from django.apps import apps
from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse

from albright_crypto_app import forms as crypto_forms
from albright_crypto_app.guards import find_secret, reject_secrets
from albright_crypto_app.models import Opportunity, Source, TimeLog

APP_DIR = Path(apps.get_app_config("albright_crypto_app").path)
APP_JS = Path(settings.BASE_DIR) / "albright_trading_app" / "static" / "js" / "crypto.js"

FORBIDDEN_NAME = re.compile(
    r"seed|mnemonic|recovery[_ ]?phrase|private[_ ]?key|priv[_ ]?key|secret|passphrase|api[_ ]?key|api[_ ]?secret"
    r"|wallet[_ ]?key|keystore|password", re.I)

# Things this app must never do. Matched against code, not comments/docstrings
# (those are stripped first), so explaining the rules doesn't trip them.
FORBIDDEN_CODE = {
    "wallet/web3 libraries": r"^\s*(import|from)\s+(web3|eth_account|eth_keys|solana|solders|bitcoinlib|bip_utils|mnemonic|ccxt)\b",
    "signing": r"sign_transaction|signTransaction|sign_message|signMessage|personal_sign|eth_sign",
    "sending transactions": r"send_raw_transaction|sendRawTransaction|sendTransaction|eth_sendTransaction",
    "connecting a wallet": r"window\.ethereum|eth_requestAccounts|walletconnect|WalletConnect|phantom\.solana",
    "token approvals": r"\.approve\(|setApprovalForAll|increaseAllowance",
    "CAPTCHA solving": r"captcha|2captcha|anticaptcha",
    "outbound POST/PUT/DELETE": r"requests\.(post|put|patch|delete)\(|httpx\.(post|put|patch|delete)\(",
}


def _code_only(path):
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        text = re.sub(r'"""[\s\S]*?"""', "", text)
        text = re.sub(r"'''[\s\S]*?'''", "", text)
        text = re.sub(r"#.*", "", text)
    elif path.suffix == ".js":
        text = re.sub(r"/\*[\s\S]*?\*/", "", text)
        text = re.sub(r"//.*", "", text)
    elif path.suffix == ".html":
        text = re.sub(r"\{#.*?#\}|\{% comment %\}[\s\S]*?\{% endcomment %\}|<!--[\s\S]*?-->", "", text)
    return text


def app_source_files():
    files = [p for p in APP_DIR.rglob("*") if p.suffix in (".py", ".html", ".js")
             and "tests" not in p.parts and "migrations" not in p.parts]
    return files + [APP_JS]


class NoSecretStorageTests(TestCase):
    def test_no_model_field_can_hold_a_secret(self):
        for model in apps.get_app_config("albright_crypto_app").get_models():
            for field in model._meta.get_fields():
                with self.subTest(model=model.__name__, field=field.name):
                    self.assertIsNone(FORBIDDEN_NAME.search(field.name))

    def test_no_form_field_can_hold_a_secret(self):
        form_classes = [obj for obj in vars(crypto_forms).values()
                        if isinstance(obj, type) and issubclass(obj, django_forms.BaseForm)
                        and obj.__module__ == crypto_forms.__name__]
        self.assertGreaterEqual(len(form_classes), 5)
        for form_class in form_classes:
            for name in form_class.base_fields:
                with self.subTest(form=form_class.__name__, field=name):
                    self.assertIsNone(FORBIDDEN_NAME.search(name))

    def test_no_crypto_setting_can_hold_a_secret(self):
        for key in settings.CRYPTO:
            self.assertIsNone(FORBIDDEN_NAME.search(key), key)
        self.assertFalse(any(FORBIDDEN_NAME.search(name) for name in dir(settings) if name.startswith("CRYPTO")))

    def test_guard_spots_wallet_secrets(self):
        words = "abandon ability able about above absent absorb abstract absurd abuse access accident"
        for text in (f"seed phrase: {words}", f"recovery words = {words}",
                     "private key: 0x" + "a" * 64, "-----BEGIN EC PRIVATE KEY-----",
                     "5HueCGU8rMjxEXxiPuD5BDku4MkFqeZyd4dZ1jvhTVqvbTLvyTJ"):
            with self.subTest(text=text[:30]):
                self.assertTrue(find_secret(text))
                with self.assertRaises(ValidationError):
                    reject_secrets(text)

    def test_guard_leaves_ordinary_text_alone(self):
        for text in ("tx hash 0x" + "b" * 64, "Fix the parser bug, $200 bounty paid on merge by the maintainers today",
                     "Don't share your seed phrase", "wallet address 0x52908400098527886E0F7030069857D2E4169EE7", ""):
            with self.subTest(text=text[:30]):
                self.assertEqual(find_secret(text), "")

    def test_secrets_refused_in_typed_fields(self):
        source = Source.objects.create(kind="manual", name="Manual", url="")
        opp = Opportunity.objects.create(source=source, external_id="x", url="https://github.com/a", title="t",
                                         notes="seed phrase: " + "word " * 11 + "word")
        with self.assertRaises(ValidationError):
            opp.full_clean()
        with self.assertRaises(ValidationError):
            TimeLog(opportunity=opp, minutes=5, note="private key 0x" + "c" * 64).full_clean()

    def test_manual_add_form_refuses_a_pasted_secret(self):
        self.client.force_login(User.objects.create_user(username="tester", password="pw-not-real-12345"))
        response = self.client.post(reverse("albright_crypto_app:add"), {
            "url": "https://github.com/a", "title": "x",
            "text": "My seed phrase: " + " ".join(["apple"] * 12)})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Never paste wallet secrets")
        self.assertFalse(Opportunity.objects.exists())


class NoWalletOrTransactionCodeTests(TestCase):
    def test_app_never_signs_sends_connects_approves_or_posts(self):
        files = app_source_files()
        self.assertTrue(any(p.name == "views.py" for p in files))
        for path in files:
            code = _code_only(path)
            if path.name == "gmail.py":
                # OAuth's own two POSTs (token exchange/refresh, revoke) - checked
                # exactly in test_gmail_posts_only_to_googles_oauth_endpoints.
                code = re.sub(r"requests\.post\((TOKEN_URL|REVOKE_URL),", "", code)
            for rule, pattern in FORBIDDEN_CODE.items():
                with self.subTest(file=path.name, rule=rule):
                    self.assertIsNone(re.search(pattern, code, re.I | re.M), f"{path.name}: {rule}")

    def test_gmail_posts_only_to_googles_oauth_endpoints(self):
        """The one exception to "no outbound POST": OAuth itself. Every POST
        in gmail.py goes to Google's token or revoke endpoint (never to the
        Gmail API), and it never PUTs, PATCHes or DELETEs anything."""
        from albright_crypto_app import gmail
        code = _code_only(APP_DIR / "gmail.py")
        posts = re.findall(r"requests\.post\(([^,]+),", code)
        self.assertTrue(posts)
        self.assertEqual(set(posts), {"TOKEN_URL", "REVOKE_URL"})
        self.assertEqual(gmail.TOKEN_URL, "https://oauth2.googleapis.com/token")
        self.assertEqual(gmail.REVOKE_URL, "https://oauth2.googleapis.com/revoke")
        self.assertNotRegex(code, r"requests\.(put|patch|delete|request)\(")
        self.assertNotIn("gmail.googleapis.com", gmail.TOKEN_URL + gmail.REVOKE_URL)

    def test_collectors_only_ever_use_get(self):
        code = _code_only(APP_DIR / "collectors" / "base.py")
        self.assertIn("requests.get(", code)
        self.assertNotRegex(code, r"requests\.(post|put|patch|delete|request)\(")


@override_settings(CRYPTO={**settings.CRYPTO, "allowlist": ["github.com"]})
class NoUncheckedExternalLinksTests(TestCase):
    def test_templates_only_render_external_urls_through_the_link_check(self):
        """No template puts an opportunity/source/tx URL straight into an
        href - they all go through {% external_link %} (or |outbound). The
        warning page's own "Continue" link is the one exception."""
        for path in (APP_DIR / "templates").rglob("*.html"):
            code = _code_only(path)
            for match in re.finditer(r'href="\{\{\s*([^}|]+?)\s*\}\}"', code):
                with self.subTest(template=path.name, href=match.group(1)):
                    self.assertTrue(path.name == "leave.html" and match.group(1) == "url",
                                    f"{path.name} links {match.group(1)} directly")
