"""Auth0 authentication for SharkNinja API."""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any, NoReturn

import aiohttp

from .const import (
    AUTH0_CLIENT_ID_EU,
    AUTH0_CLIENT_ID_US,
    AUTH0_TOKEN_URL_EU,
    AUTH0_TOKEN_URL_US,
    LOGGER,
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
    """Username or password is incorrect."""


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
        username: str,
        password: str,
        websession: aiohttp.ClientSession,
        *,
        europe: bool = False,
    ) -> None:
        """Initialize the auth handler."""
        self._username = username
        self._password = password
        self._websession = websession
        self._europe = europe

        self._token_url = AUTH0_TOKEN_URL_EU if europe else AUTH0_TOKEN_URL_US
        self._client_id = AUTH0_CLIENT_ID_EU if europe else AUTH0_CLIENT_ID_US

        self._id_token: str | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._token_expiry: float = 0

    @property
    def id_token(self) -> str | None:
        """Return the current ID token."""
        return self._id_token

    @property
    def user_id(self) -> str | None:
        """Extract user ID from the JWT id_token sub claim."""
        if not self._id_token:
            return None
        try:
            # Decode JWT payload without verification (we trust Auth0)
            payload = self._id_token.split(".")[1]
            # Add padding
            padding = 4 - len(payload) % 4
            if padding != 4:
                payload += "=" * padding
            claims = json.loads(base64.urlsafe_b64decode(payload))
            sub = claims.get("sub", "")
            # Auth0 sub format is "auth0|<user_id>"
            return sub.split("|", 1)[1] if "|" in sub else sub
        except (IndexError, json.JSONDecodeError, ValueError):
            LOGGER.error("Failed to extract user_id from id_token")
            return None

    @property
    def token_expiring_soon(self) -> bool:
        """Check if the token is expiring within 10 minutes."""
        return time.time() > self._token_expiry - 600

    @property
    def is_authenticated(self) -> bool:
        """Check if we have valid tokens."""
        return self._id_token is not None and time.time() < self._token_expiry

    async def async_sign_in(self) -> None:
        """Authenticate via Auth0 password grant."""
        data = {
            "grant_type": "http://auth0.com/oauth/grant-type/password-realm",
            "realm": "Username-Password-Authentication",
            "client_id": self._client_id,
            "username": self._username,
            "password": self._password,
            "scope": "openid email profile offline_access",
        }
        await self._async_token_request(data)

    async def async_refresh_auth(self) -> None:
        """Refresh authentication using the stored refresh token."""
        if not self._refresh_token:
            LOGGER.debug("No refresh token available, performing full sign-in")
            await self.async_sign_in()
            return

        data = {
            "grant_type": "refresh_token",
            "client_id": self._client_id,
            "refresh_token": self._refresh_token,
        }
        try:
            await self._async_token_request(data)
        except SharkAuthError:
            LOGGER.debug("Refresh token failed, falling back to full sign-in")
            await self.async_sign_in()

    async def _async_token_request(self, data: dict[str, str]) -> None:
        """Execute a token request against Auth0."""
        try:
            async with self._websession.post(
                self._token_url,
                json=data,
                headers={"content-type": "application/json"},
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
        - ``invalid_grant``: username/password actually wrong
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
                "Username or password incorrect"
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
