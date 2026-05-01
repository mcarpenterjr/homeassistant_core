"""Auth0 authentication for SharkNinja API."""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

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
                if resp.status == 403:
                    body = await resp.text()
                    LOGGER.error("Auth0 returned 403: %s", body)
                    raise SharkAuthError(
                        "Auth0 blocked request (possible anti-bot detection)"
                    )
                if resp.status != 200:
                    body = await resp.text()
                    LOGGER.error("Auth0 returned %d: %s", resp.status, body)
                    raise SharkAuthError(f"Auth0 returned status {resp.status}")

                result: dict[str, Any] = await resp.json()
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
