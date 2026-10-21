"""Gmail, read-only: OAuth with the gmail.readonly scope and nothing else,
and a client that only ever GETs (labels, message list, one message).
Nothing here - or anywhere in this app - sends, deletes, labels, archives
or otherwise modifies an email.

The official Gmail REST API and Google's OAuth 2.0 endpoints are called
directly with requests (no extra Google packages). The OAuth client comes
from settings GOOGLE_OAUTH_CLIENT_ID / GOOGLE_OAUTH_CLIENT_SECRET; the
token is stored encrypted (models.GmailConnection, Fernet - same key as the
Alpaca credentials).
"""
import base64
import html
import re
import time
from urllib.parse import urlencode

import requests
from django.conf import settings

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"   # the only scope ever requested or accepted
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT = 20
MAX_LINKS = 40


class GmailError(Exception):
    """Not connected, not configured, or Google refused - shown as a message."""


def client_config():
    client_id = getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    secret = getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")
    if not client_id or not secret:
        raise GmailError("Gmail isn't set up on this server yet: set GOOGLE_OAUTH_CLIENT_ID and "
                         "GOOGLE_OAUTH_CLIENT_SECRET (a Google Cloud OAuth client of type \"Web application\").")
    return client_id, secret


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

def authorization_url(redirect_uri, state):
    client_id, _secret = client_config()
    return AUTH_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false", "state": state,
    })


def _check_scope(token):
    granted = set((token.get("scope") or "").split())
    if granted != {SCOPE}:
        raise GmailError(f"Google granted {sorted(granted) or 'no scopes'}; only gmail.readonly is accepted. "
                         "Nothing was saved - try connecting again.")


def exchange_code(code, redirect_uri):
    """The OAuth callback's code -> token dict (with expires_at). Rejects
    any grant that isn't exactly gmail.readonly."""
    client_id, secret = client_config()
    resp = requests.post(TOKEN_URL, data={"code": code, "client_id": client_id, "client_secret": secret,
                                          "redirect_uri": redirect_uri, "grant_type": "authorization_code"},
                         timeout=TIMEOUT)
    if resp.status_code != 200:
        raise GmailError("Google didn't accept the sign-in. Try connecting again.")
    token = resp.json()
    _check_scope(token)
    if not token.get("refresh_token"):
        raise GmailError("Google didn't return a refresh token. Remove the app's access in your Google account "
                         "settings, then connect again.")
    token["expires_at"] = time.time() + int(token.get("expires_in") or 0)
    return token


def refresh(token):
    client_id, secret = client_config()
    resp = requests.post(TOKEN_URL, data={"client_id": client_id, "client_secret": secret,
                                          "refresh_token": token["refresh_token"], "grant_type": "refresh_token"},
                         timeout=TIMEOUT)
    if resp.status_code != 200:
        raise GmailError("Gmail access was revoked or expired - reconnect on the Email alerts page.")
    fresh = resp.json()
    if fresh.get("scope"):
        _check_scope(fresh)
    return {**token, "access_token": fresh["access_token"], "expires_at": time.time() + int(fresh.get("expires_in") or 0)}


def revoke(token):
    """Best effort, on disconnect (revoking access doesn't touch any email)."""
    try:
        requests.post(REVOKE_URL, data={"token": token.get("refresh_token") or token.get("access_token")},
                      timeout=TIMEOUT)
    except requests.RequestException:
        pass


# ---------------------------------------------------------------------------
# Read-only client
# ---------------------------------------------------------------------------

class GmailReader:
    """GET-only access to the connected mailbox. connection is a
    models.GmailConnection; a refreshed access token is saved back to it."""

    def __init__(self, connection):
        self.connection = connection
        self.token = connection.get_token()

    def _access_token(self):
        if time.time() > float(self.token.get("expires_at") or 0) - 60:
            self.token = refresh(self.token)
            self.connection.set_token(self.token)
            self.connection.save(update_fields=["encrypted_token", "updated_at"])
        return self.token["access_token"]

    def _get(self, path, params=None):
        resp = requests.get(f"{API}/{path}", params=params, timeout=TIMEOUT,
                            headers={"Authorization": f"Bearer {self._access_token()}"})
        if resp.status_code in (401, 403):
            raise GmailError("Gmail refused the request - reconnect on the Email alerts page.")
        resp.raise_for_status()
        return resp.json()

    def profile_email(self):
        return self._get("profile").get("emailAddress", "")

    def label_id(self, name):
        for label in self._get("labels").get("labels") or []:
            if (label.get("name") or "").lower() == (name or "").lower():
                return label["id"]
        return None

    def message_ids(self, label_id, max_results=25):
        """Newest first."""
        data = self._get("messages", {"labelIds": label_id, "maxResults": max_results})
        return [m["id"] for m in data.get("messages") or []]

    def message(self, message_id):
        return self._get(f"messages/{message_id}", {"format": "full"})


# ---------------------------------------------------------------------------
# Message parsing
# ---------------------------------------------------------------------------

def _decode(data):
    return base64.urlsafe_b64decode((data or "") + "=" * (-len(data or "") % 4)).decode("utf-8", errors="replace")


def _parts(payload):
    yield payload
    for part in payload.get("parts") or []:
        yield from _parts(part)


def header(message, name):
    for h in (message.get("payload") or {}).get("headers") or []:
        if (h.get("name") or "").lower() == name.lower():
            return h.get("value") or ""
    return ""


def html_to_text(markup):
    """HTML -> text, writing each link as "label (url)" so postings keep their links."""
    markup = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", markup or "")
    markup = re.sub(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                    lambda m: f"{re.sub(r'<[^>]+>', ' ', m.group(2)).strip()} ({html.unescape(m.group(1))})", markup)
    markup = re.sub(r"(?i)<(br|/p|/div|/li|/tr|/h\d)\s*/?>", "\n", markup)
    text = html.unescape(re.sub(r"<[^>]+>", " ", markup))
    text = re.sub(r"[ \t\xa0]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def message_text(message):
    """(subject, body text). Plain text preferred; HTML converted otherwise."""
    plain, rich = [], []
    for part in _parts(message.get("payload") or {}):
        mime = (part.get("mimeType") or "").lower()
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            rich.append(html_to_text(_decode(data)))
    body = "\n\n".join(plain) if plain else "\n\n".join(rich)
    return header(message, "Subject"), body.strip()


URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def links_in(text):
    """Distinct http(s) links in order of appearance (at most MAX_LINKS)."""
    seen = []
    for url in URL_RE.findall(text or ""):
        url = url.rstrip(".,;:")
        if url not in seen:
            seen.append(url)
    return seen[:MAX_LINKS]
