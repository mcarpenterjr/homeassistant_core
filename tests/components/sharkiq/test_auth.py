"""Tests for the Shark IQ Auth0 authentication module."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from homeassistant.components.sharkiq.auth import (
    SharkAuth,
    SharkAuthError,
    SharkAuthInvalidCredentialsError,
    SharkAuthRateLimitedError,
    SharkAuthVerificationRequiredError,
    raise_if_auth0_account_blocked,
)


def _build_session(status: int, body: str) -> MagicMock:
    """Return a websession whose ``post`` yields a single canned response."""
    response = MagicMock()
    response.status = status
    response.text = AsyncMock(return_value=body)
    response.json = AsyncMock(
        side_effect=lambda: json.loads(body) if body else {}
    )
    response.__aenter__ = AsyncMock(return_value=response)
    response.__aexit__ = AsyncMock(return_value=None)

    session = MagicMock(spec=aiohttp.ClientSession)
    session.post = MagicMock(return_value=response)
    return session


@pytest.mark.parametrize(
    ("status", "body", "expected_exc"),
    [
        # Auth0 anti-fraud signals on the account or IP. Status is 401, but the
        # ``error`` field — not the status — is what tells us to flip into the
        # interactive-verification recovery path.
        (
            401,
            '{"error":"requires_verification","error_description":"Suspicious request"}',
            SharkAuthVerificationRequiredError,
        ),
        # Same anti-fraud category, different signal: account got hit by too
        # many failed login attempts. We want backoff, not reauth.
        (
            429,
            '{"error":"too_many_attempts","error_description":"blocked"}',
            SharkAuthRateLimitedError,
        ),
        # Auth0 emits ``too_many_attempts`` with status 401 in some tenants too.
        (
            401,
            '{"error":"too_many_attempts","error_description":"blocked"}',
            SharkAuthRateLimitedError,
        ),
        # Wrong username/password. Distinct from anti-fraud because the user
        # actually needs to fix their credentials, not their network.
        (
            401,
            '{"error":"invalid_grant","error_description":"Wrong email or password"}',
            SharkAuthInvalidCredentialsError,
        ),
        # 403 without a recognised ``error`` field — bot-detection on the
        # *request* (heuristic on headers/IP), not the account.
        (403, '{"error":"unauthorized_client"}', SharkAuthError),
        # Status outside known ranges + non-JSON body still produces a clean
        # error rather than a ValueError on json.loads.
        (500, "<html>oops</html>", SharkAuthError),
        # Empty body — must not raise the JSON decode error to the caller.
        (502, "", SharkAuthError),
    ],
)
async def test_async_sign_in_classifies_auth0_errors(
    status: int, body: str, expected_exc: type[SharkAuthError]
) -> None:
    """Each Auth0 failure mode maps to its specific exception subclass."""
    session = _build_session(status, body)
    auth = SharkAuth(
        username="u@example.com",
        password="pw",
        websession=session,
        europe=False,
    )

    with pytest.raises(expected_exc):
        await auth.async_sign_in()


async def test_async_sign_in_success_populates_tokens() -> None:
    """Happy path stores id_token, refresh_token, and expiry."""
    body = json.dumps(
        {
            "id_token": "eyJhbGciOi.eyJzdWIiOiJhdXRoMHwxMjMifQ.sig",
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_in": 86400,
        }
    )
    session = _build_session(200, body)
    auth = SharkAuth(
        username="u@example.com",
        password="pw",
        websession=session,
        europe=False,
    )

    await auth.async_sign_in()

    assert auth.id_token == "eyJhbGciOi.eyJzdWIiOiJhdXRoMHwxMjMifQ.sig"
    assert auth.is_authenticated is True
    # JWT sub claim is "auth0|123" — user_id should strip the "auth0|" prefix.
    assert auth.user_id == "123"


async def test_async_sign_in_sends_sharkclean_app_fingerprint() -> None:
    """Token requests carry SharkClean-app-style headers, not naked JSON.

    Auth0's anti-fraud is trained against the real Android app's request
    fingerprint. A POST that's missing User-Agent / Origin / Referer scores
    high enough to trip a ``requires_verification`` challenge even on an
    otherwise-clean account, so we explicitly mimic the app's headers. If
    this test starts failing it means the fingerprint regressed — Auth0
    will likely re-flag the account in production until it's restored.
    """
    body = json.dumps(
        {
            "id_token": "eyJhbGciOi.eyJzdWIiOiJ4In0.sig",
            "refresh_token": "r",
            "expires_in": 86400,
        }
    )
    session = _build_session(200, body)
    auth = SharkAuth(
        username="u@example.com",
        password="pw",
        websession=session,
        europe=False,
    )

    await auth.async_sign_in()

    # Inspect what was actually sent to the post call.
    _args, kwargs = session.post.call_args
    headers = kwargs["headers"]
    assert headers["Content-Type"] == "application/json"
    assert "User-Agent" in headers
    # Specifically: a mobile-app-shaped UA, not the python aiohttp default.
    assert "Mobile" in headers["User-Agent"] or "Android" in headers["User-Agent"]
    # Origin/Referer must point at the Auth0 host the token URL targets, not
    # be hard-coded — different regions use different hosts.
    assert headers["Origin"] == "https://login.sharkninja.com"
    assert headers["Referer"] == "https://login.sharkninja.com/"


async def test_async_sign_in_origin_follows_eu_token_url() -> None:
    """EU accounts hit ``logineu.sharkninja.com``; Origin/Referer must match."""
    body = json.dumps(
        {
            "id_token": "eyJhbGciOi.eyJzdWIiOiJ4In0.sig",
            "expires_in": 86400,
        }
    )
    session = _build_session(200, body)
    auth = SharkAuth(
        username="u@example.com",
        password="pw",
        websession=session,
        europe=True,
    )

    await auth.async_sign_in()

    _args, kwargs = session.post.call_args
    headers = kwargs["headers"]
    assert headers["Origin"] == "https://logineu.sharkninja.com"


async def test_async_sign_in_network_error_wrapped() -> None:
    """Transport-layer errors are wrapped as SharkAuthError, not bubbled raw."""
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post = MagicMock(
        side_effect=aiohttp.ClientConnectorError(MagicMock(), OSError("boom"))
    )
    auth = SharkAuth(
        username="u@example.com",
        password="pw",
        websession=session,
        europe=False,
    )

    with pytest.raises(SharkAuthError) as exc_info:
        await auth.async_sign_in()
    # Specific subclasses are reserved for Auth0-tagged failures, so a generic
    # network error must surface as the base class — callers use that to decide
    # whether to retry a different backend.
    assert type(exc_info.value) is SharkAuthError


async def test_refresh_falls_back_to_full_signin_on_failure() -> None:
    """If the refresh-token request fails, retry with password grant."""
    auth = SharkAuth(
        username="u@example.com",
        password="pw",
        websession=MagicMock(spec=aiohttp.ClientSession),
        europe=False,
    )
    # Pretend we have a refresh token from a previous sign-in.
    auth._refresh_token = "stale-refresh-token"  # noqa: SLF001

    # First call (refresh attempt) blows up; second call (sign_in fallback)
    # succeeds. We assert sign_in is invoked exactly once after the failure.
    with patch.object(
        auth,
        "_async_token_request",
        side_effect=[SharkAuthError("refresh denied"), None],
    ) as token_request:
        await auth.async_refresh_auth()

    assert token_request.call_count == 2
    # Second call should be a password grant, not another refresh.
    second_payload: dict[str, Any] = token_request.call_args_list[1].args[0]
    assert second_payload["grant_type"].endswith("password-realm")


@pytest.mark.parametrize(
    ("message", "expected_exc"),
    [
        # Skegox path uses underscore form (response JSON ``error`` field).
        ("Auth0 returned 401: {\"error\":\"requires_verification\"}", SharkAuthVerificationRequiredError),
        # Pip lib's auth0.exceptions.Auth0Error message uses the human form.
        ("401: Suspicious request requires verification", SharkAuthVerificationRequiredError),
        # The phrase "requires verification" alone is enough — different libs
        # reformat the same Auth0 response in different ways.
        ("Authentication failed: requires verification", SharkAuthVerificationRequiredError),
        # 429 path — rate limiter.
        ('{"error":"too_many_attempts"}', SharkAuthRateLimitedError),
        # Pip lib surfaces 429 with this exact phrase from the Ayla response.
        (
            "429: Your account has been blocked after multiple consecutive login attempts.",
            SharkAuthRateLimitedError,
        ),
    ],
)
def test_raise_if_auth0_account_blocked_classifies_messages(
    message: str, expected_exc: type[SharkAuthError]
) -> None:
    """Sniff the message string of an arbitrary exception, raise the typed form."""
    with pytest.raises(expected_exc):
        raise_if_auth0_account_blocked(Exception(message))


@pytest.mark.parametrize(
    "message",
    [
        # Generic auth failure that is NOT Auth0 anti-fraud — caller should
        # be free to fall through to the other backend.
        "Username or password incorrect",
        # Network problem — definitely not an account-level signal.
        "Network is unreachable",
        # Empty / no message at all.
        "",
        # Word-similarity false positive guard: "verification" only triggers
        # when paired with the Auth0-specific phrasing, not in arbitrary text.
        "Email verification not required for this user",
    ],
)
def test_raise_if_auth0_account_blocked_passes_unrelated_errors(message: str) -> None:
    """No-op when the message doesn't match a known Auth0 account-level pattern."""
    # Should return cleanly; the caller is then responsible for whatever
    # generic handling applies (fall through, retry, or surface as invalid_auth).
    raise_if_auth0_account_blocked(Exception(message))
