"""Auth0 authentication for SharkNinja API.

The integration uses PKCE / authorization-code-with-paste rather than the
password-realm grant. The user opens an ``/authorize`` URL in their own
browser, completes login (and any Auth0 anti-fraud verification) there,
then pastes the resulting redirect URL back into the config flow. This is
the only flow that reliably gets past Auth0's ``requires_verification``
challenge, because the challenge is meant to be answered interactively —
which a server cannot do but a browser can. Once we have the initial
refresh token, ``grant_type=refresh_token`` works server-side without
re-triggering verification.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any, NoReturn
from urllib.parse import parse_qs, urlencode, urlparse

import aiohttp

from .const import (
    AUTH0_AUTHORIZE_URL_EU,
    AUTH0_AUTHORIZE_URL_US,
    AUTH0_CLIENT_ID_EU,
    AUTH0_CLIENT_ID_US,
    AUTH0_SCOPES,
    AUTH0_TOKEN_URL_EU,
    AUTH0_TOKEN_URL_US,
    LOGGER,
    SHARKCLEAN_REDIRECT_URI,
)

# Mimic the SharkClean Android app's request fingerprint. Auth0's anti-fraud
# trains on real app traffic — a naked POST with no User-Agent from a server
# IP scores high enough to trip ``requires_verification``, even on a clean
# account. Matches the headers the upstream ``sharkiq`` PyPI lib's auth0
# client uses, which avoids cross-implementation drift.
_SHARKCLEAN_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/139.0.0.0 Mobile Safari/537.36"
)


class SharkAuthError(Exception):
    """Base authentication error."""


class SharkAuthExpiredError(SharkAuthError):
    """Token has expired."""


class SharkAuthVerificationRequiredError(SharkAuthError):
    """Auth0 anti-fraud has flagged this account/IP and requires interactive verification.

    Cleared by signing into the SharkClean mobile app from the same network as
    Home Assistant. Also raised by the legacy Ayla path because both backends
    share the same Auth0 tenant.
    """


class SharkAuthRateLimitedError(SharkAuthError):
    """Auth0 has temporarily blocked the account after too many failed attempts."""


class SharkAuthInvalidCredentialsError(SharkAuthError):
    """Auth0 rejected the grant (stale refresh token or invalid PKCE code)."""


def generate_pkce_pair() -> tuple[str, str]:
    """Return a fresh ``(code_verifier, code_challenge)`` pair for PKCE.

    RFC 7636 specifies code_verifier as 43-128 chars from the URL-safe
    alphabet, and code_challenge as the base64url-encoded SHA-256 of the
    verifier with padding stripped.
    """
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    return verifier, challenge


def build_authorize_url(
    *, europe: bool, code_challenge: str, state: str
) -> str:
    """Build the URL the user opens in their browser to start login."""
    base = AUTH0_AUTHORIZE_URL_EU if europe else AUTH0_AUTHORIZE_URL_US
    client_id = AUTH0_CLIENT_ID_EU if europe else AUTH0_CLIENT_ID_US
    params = {
        "response_type": "code",
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
        "client_id": client_id,
        "redirect_uri": SHARKCLEAN_REDIRECT_URI,
        "scope": AUTH0_SCOPES,
        "state": state,
        # ``prompt=login`` forces Auth0 to re-prompt even if the user has
        # an existing session — important for reauth flows where the
        # stored tokens have already failed.
        "prompt": "login",
    }
    return f"{base}?{urlencode(params)}"


def parse_token_blob(text: str) -> tuple[str, str]:
    """Extract ``(refresh_token, id_token)`` from a shark2mqtt token JSON paste.

    `shark2mqtt <https://github.com/CamSoper/shark2mqtt>`_ runs Chromium in
    a Docker container to do the PKCE login automatically and persists the
    resulting tokens to ``shark2mqtt_tokens.json``. Users who'd rather run
    that one-shot than do the manual paste can hand us the file's contents
    directly — same end-state tokens.

    Raises ValueError if the input isn't a recognisable token blob; callers
    fall back to the redirect-URL / bare-code path.
    """
    text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        raise ValueError("not a JSON object")
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as err:
        raise ValueError("paste looks like JSON but failed to parse") from err
    if not isinstance(data, dict):
        raise ValueError("token blob must be a JSON object")
    refresh = data.get("auth0_refresh_token")
    id_token = data.get("auth0_id_token")
    if not isinstance(refresh, str) or not refresh:
        raise ValueError("missing or empty auth0_refresh_token")
    if not isinstance(id_token, str) or not id_token:
        raise ValueError("missing or empty auth0_id_token")
    return refresh, id_token


def parse_callback_input(text: str) -> tuple[str, str | None]:
    """Extract ``(code, state)`` from a pasted redirect URL or bare code.

    Browsers can't launch the ``com.sharkninja.shark://`` scheme, so the
    user copies the URL bar value (which still contains the query string)
    or just the ``code`` parameter from it. We accept either shape.

    Raises ValueError if no code can be extracted, including the case of a
    URL whose query string has no ``code`` parameter (e.g. an
    ``error=access_denied`` redirect from a cancelled login).
    """
    text = text.strip()
    if not text:
        raise ValueError("empty input")

    # URL-shaped inputs are anything with a ``?`` or a scheme separator.
    # Branch on shape so we don't mistakenly treat an error-redirect URL
    # as a bare authorization code (URLs have ``=`` everywhere).
    if "?" in text or "://" in text:
        query_start = text.find("?")
        if query_start < 0:
            raise ValueError("URL has no query string")
        params = parse_qs(text[query_start + 1 :])
        codes = params.get("code", [])
        if not codes:
            raise ValueError("URL has no code parameter")
        states = params.get("state", [])
        return codes[0], states[0] if states else None

    # Otherwise treat as a bare authorization code.
    return text, None


def raise_if_auth0_account_blocked(err: BaseException) -> None:
    """Re-raise as a specific Auth0 account-level error if the message matches.

    Both the skegox path and the legacy Ayla path (the ``sharkiq`` PyPI lib)
    talk to the same Auth0 tenant, but they wrap failures differently — and
    the pip lib lets ``auth0.exceptions.Auth0Error`` leak through unwrapped on
    its cookie fallback. Sniff the message text so all three call sites can
    route account-level Auth0 failures through the same reauth/backoff logic.
    No-op if the error doesn't match a known pattern.
    """
    msg = str(err).lower()
    if (
        "requires_verification" in msg
        or "requires verification" in msg
        or "suspicious request" in msg
    ):
        raise SharkAuthVerificationRequiredError(str(err)) from err
    if "too_many_attempts" in msg or "blocked after multiple" in msg:
        raise SharkAuthRateLimitedError(str(err)) from err


class SharkAuth:
    """Handle Auth0 authentication for SharkNinja."""

    def __init__(
        self,
        websession: aiohttp.ClientSession,
        *,
        europe: bool = False,
        refresh_token: str | None = None,
        id_token: str | None = None,
        token_expiry: float = 0,
    ) -> None:
        """Initialize the auth handler.

        Construct without tokens for a fresh PKCE flow (call
        :meth:`async_exchange_code` after the user pastes the redirect),
        or pass ``refresh_token``/``id_token``/``token_expiry`` to
        restore from previously-persisted state.
        """
        self._websession = websession
        self._europe = europe

        self._token_url = AUTH0_TOKEN_URL_EU if europe else AUTH0_TOKEN_URL_US
        self._client_id = AUTH0_CLIENT_ID_EU if europe else AUTH0_CLIENT_ID_US
        # Origin/Referer for the SharkClean-app fingerprint — derived from the
        # token URL so we don't drift if the host ever changes.
        parsed = urlparse(self._token_url)
        self._auth_origin = f"{parsed.scheme}://{parsed.netloc}"

        self._id_token: str | None = id_token
        self._access_token: str | None = None
        self._refresh_token: str | None = refresh_token
        self._token_expiry: float = token_expiry

    @property
    def id_token(self) -> str | None:
        """Return the current ID token."""
        return self._id_token

    @property
    def refresh_token(self) -> str | None:
        """Return the current refresh token."""
        return self._refresh_token

    @property
    def token_expiry(self) -> float:
        """Return the unix timestamp the access token expires at."""
        return self._token_expiry

    def _decode_id_token_claims(self) -> dict[str, Any]:
        """Decode the JWT payload without verification (we trust Auth0).

        Returns ``{}`` on any decode error so callers can use ``.get()``
        without further defensive checks.
        """
        if not self._id_token:
            return {}
        try:
            payload = self._id_token.split(".")[1]
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            return json.loads(base64.urlsafe_b64decode(payload))
        except (IndexError, json.JSONDecodeError, ValueError):
            LOGGER.error("Failed to decode id_token claims")
            return {}

    @property
    def user_id(self) -> str | None:
        """Extract user ID from the JWT id_token sub claim."""
        sub = self._decode_id_token_claims().get("sub", "")
        if not sub:
            return None
        # Auth0 sub format is "auth0|<user_id>"
        return sub.split("|", 1)[1] if "|" in sub else sub

    @property
    def email(self) -> str | None:
        """Extract email from the JWT id_token, if present."""
        email = self._decode_id_token_claims().get("email")
        return email if isinstance(email, str) and email else None

    @property
    def token_expiring_soon(self) -> bool:
        """Check if the token is expiring within 10 minutes."""
        return time.time() > self._token_expiry - 600

    @property
    def is_authenticated(self) -> bool:
        """Check if we have valid tokens."""
        return self._id_token is not None and time.time() < self._token_expiry

    async def async_exchange_code(
        self, code: str, code_verifier: str
    ) -> None:
        """Exchange a PKCE authorization code for tokens.

        Called once after the user pastes the redirect URL from their
        browser. Populates id_token + refresh_token; from then on,
        :meth:`async_refresh_auth` keeps the access token current.
        """
        data = {
            "grant_type": "authorization_code",
            "client_id": self._client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": SHARKCLEAN_REDIRECT_URI,
        }
        await self._async_token_request(data)

    async def async_refresh_auth(self) -> None:
        """Refresh authentication using the stored refresh token.

        Unlike a fresh login, refresh requests don't trigger Auth0's
        ``requires_verification`` challenge — the refresh token itself
        proves a previously-completed valid login. There's no fallback
        path: if the refresh token is invalid the caller must restart
        the PKCE flow, which they do by raising ``ConfigEntryAuthFailed``
        from setup.
        """
        if not self._refresh_token:
            raise SharkAuthError(
                "No refresh token available; user must complete PKCE flow"
            )

        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }
        await self._async_token_request(data)

    async def _async_token_request(self, data: dict[str, str]) -> None:
        """Execute a token request against Auth0."""
        headers = {
            "Content-Type": "application/json",
            "User-Agent": _SHARKCLEAN_USER_AGENT,
            "Origin": self._auth_origin,
            "Referer": f"{self._auth_origin}/",
        }
        try:
            async with self._websession.post(
                self._token_url,
                json=data,
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    result: dict[str, Any] = await resp.json()
                else:
                    body = await resp.text()
                    LOGGER.error("Auth0 returned %d: %s", resp.status, body)
                    self._raise_for_auth0_error(resp.status, body)
        except aiohttp.ClientError as err:
            raise SharkAuthError(f"Network error during auth: {err}") from err

        if "id_token" not in result:
            raise SharkAuthError("Auth0 response missing id_token")

        self._id_token = result["id_token"]
        self._access_token = result.get("access_token")
        if "refresh_token" in result:
            self._refresh_token = result["refresh_token"]
        expires_in = result.get("expires_in", 86400)
        self._token_expiry = time.time() + expires_in
        LOGGER.debug("Auth0 authentication successful, token expires in %ds", expires_in)

    @staticmethod
    def _raise_for_auth0_error(status: int, body: str) -> NoReturn:
        """Translate an Auth0 error response into a specific exception.

        Auth0 encodes failure modes in the response JSON's ``error`` field rather
        than the HTTP status, so callers can recover differently per cause:

        - ``requires_verification`` (401): account flagged by Auth0 anti-fraud,
          must be cleared interactively via the SharkClean mobile app
        - ``too_many_attempts``/429: too many failed logins, wait or unblock
        - ``invalid_grant``: refresh token stale, or PKCE code already used
        - 403 with no recognised error: anti-bot heuristic on the *request*
        """
        err_code = ""
        try:
            err_json = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            err_json = None
        if isinstance(err_json, dict):
            err_code = err_json.get("error", "") or ""

        if err_code == "requires_verification":
            raise SharkAuthVerificationRequiredError(
                "Auth0 verification required - sign in via the SharkClean mobile "
                "app on the same network as Home Assistant to clear the challenge"
            )
        if err_code == "too_many_attempts" or status == 429:
            raise SharkAuthRateLimitedError(
                "Account temporarily blocked due to too many failed login attempts"
            )
        if err_code == "invalid_grant":
            raise SharkAuthInvalidCredentialsError(
                "Auth0 rejected the grant (stale refresh token or invalid "
                "authorization code) - reauthenticate via the integration"
            )
        if status == 403:
            raise SharkAuthError(
                "Auth0 blocked request (possible anti-bot detection)"
            )
        raise SharkAuthError(f"Auth0 returned status {status}")

    def build_signature_header(self, timestamp: str) -> str:
        """Build a fake SN-HMAC-SHA256 signature header.

        The server requires this header to be present but does not validate
        the actual signature value - only the Bearer token and API key are checked.
        """
        return (
            f"SN-HMAC-SHA256 Credential=x/{timestamp}/*/end-user-api/sn_request, "
            f"SignedHeaders=host;x-sn-date;x-sn-nonce, "
            f"Signature={secrets.token_hex(32)}"
        )
