"""Tests for the Shark IQ Auth0 authentication module."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest

from homeassistant.components.sharkiq.auth import (
    SharkAuth,
    SharkAuthError,
    SharkAuthInvalidCredentialsError,
    SharkAuthRateLimitedError,
    SharkAuthVerificationRequiredError,
    build_authorize_url,
    generate_pkce_pair,
    parse_callback_input,
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


def _new_auth(websession: aiohttp.ClientSession, *, europe: bool = False) -> SharkAuth:
    """Construct a fresh SharkAuth with no stored tokens."""
    return SharkAuth(websession=websession, europe=europe)


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------


def test_generate_pkce_pair_meets_rfc7636() -> None:
    """Verifier is 43-128 URL-safe chars, challenge is its sha256 base64url.

    Auth0 strictly validates the challenge derivation — even a one-byte
    encoding bug means every authorization_code exchange fails with
    invalid_grant. Lock this in.
    """
    verifier, challenge = generate_pkce_pair()

    assert 43 <= len(verifier) <= 128
    # URL-safe alphabet only — RFC 7636 §4.1 requires no = padding either.
    assert all(c.isalnum() or c in "-._~" for c in verifier)

    # Challenge is base64url(sha256(verifier)) with padding stripped.
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert challenge == expected


def test_generate_pkce_pair_returns_unique_values() -> None:
    """Each call must produce a fresh pair — reused PKCE state defeats the protection."""
    pairs = {generate_pkce_pair() for _ in range(20)}
    assert len(pairs) == 20


# ---------------------------------------------------------------------------
# Authorize URL construction
# ---------------------------------------------------------------------------


def test_build_authorize_url_us_includes_required_params() -> None:
    """The /authorize URL must carry every PKCE-required parameter Auth0 demands."""
    url = build_authorize_url(
        europe=False, code_challenge="abc-challenge", state="xyz-state"
    )
    parsed = urlparse(url)
    params = parse_qs(parsed.query)

    assert parsed.netloc == "login.sharkninja.com"
    assert parsed.path == "/authorize"
    assert params["response_type"] == ["code"]
    assert params["code_challenge_method"] == ["S256"]
    assert params["code_challenge"] == ["abc-challenge"]
    assert params["state"] == ["xyz-state"]
    # Custom iOS scheme — desktop browsers can't launch it but still surface
    # the URL with the code, which is the whole point of the paste flow.
    assert params["redirect_uri"][0].startswith("com.sharkninja.shark://")
    assert "openid" in params["scope"][0]
    assert "offline_access" in params["scope"][0]
    # ``prompt=login`` forces a fresh login even with an existing session;
    # important for reauth where the stored tokens have already gone bad.
    assert params["prompt"] == ["login"]


def test_build_authorize_url_eu_uses_eu_host() -> None:
    """EU accounts must hit logineu.sharkninja.com or auth fails opaquely."""
    url = build_authorize_url(
        europe=True, code_challenge="x", state="y"
    )
    assert urlparse(url).netloc == "logineu.sharkninja.com"


# ---------------------------------------------------------------------------
# Callback parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected_code", "expected_state"),
    [
        # Full redirect URL the browser ends up on after Auth0's redirect.
        (
            "com.sharkninja.shark://login.sharkninja.com/ios/com.sharkninja.shark/callback?code=AUTH_CODE_123&state=STATE_456",
            "AUTH_CODE_123",
            "STATE_456",
        ),
        # User pasted just the query string (some browsers truncate the scheme).
        ("?code=AUTH_CODE_123&state=STATE_456", "AUTH_CODE_123", "STATE_456"),
        # Bare code without state — accepted but state validation skipped.
        ("AUTH_CODE_123", "AUTH_CODE_123", None),
        # Whitespace tolerance — users copy-paste from terminals or chat apps.
        ("   AUTH_CODE_123   ", "AUTH_CODE_123", None),
        # URL with code but no state (some Auth0 configs).
        (
            "com.sharkninja.shark://callback?code=ONLY_CODE",
            "ONLY_CODE",
            None,
        ),
    ],
)
def test_parse_callback_input_accepts_known_shapes(
    text: str, expected_code: str, expected_state: str | None
) -> None:
    """Every shape a real user might paste should produce a usable code."""
    code, state = parse_callback_input(text)
    assert code == expected_code
    assert state == expected_state


@pytest.mark.parametrize(
    "text",
    [
        "",
        "   ",
        "https://example.com/callback?error=access_denied",  # has ``=`` but no ``code=``
    ],
)
def test_parse_callback_input_rejects_codeless_input(text: str) -> None:
    """Inputs with no extractable code must raise so the form can re-prompt."""
    with pytest.raises(ValueError):
        parse_callback_input(text)


# ---------------------------------------------------------------------------
# Token exchange — error classification
# ---------------------------------------------------------------------------


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
        # many failed login attempts.
        (
            429,
            '{"error":"too_many_attempts","error_description":"blocked"}',
            SharkAuthRateLimitedError,
        ),
        (
            401,
            '{"error":"too_many_attempts","error_description":"blocked"}',
            SharkAuthRateLimitedError,
        ),
        # ``invalid_grant`` is the canonical PKCE-flow error for an expired
        # or replayed authorization code.
        (
            401,
            '{"error":"invalid_grant","error_description":"Code expired"}',
            SharkAuthInvalidCredentialsError,
        ),
        # 403 without a recognised ``error`` field — bot-detection on the
        # *request* (heuristic on headers/IP), not the account.
        (403, '{"error":"unauthorized_client"}', SharkAuthError),
        # Status outside known ranges + non-JSON body still produces a clean
        # error rather than a ValueError on json.loads.
        (500, "<html>oops</html>", SharkAuthError),
        # Empty body — must not raise a JSON decode error to the caller.
        (502, "", SharkAuthError),
    ],
)
async def test_async_exchange_code_classifies_auth0_errors(
    status: int, body: str, expected_exc: type[SharkAuthError]
) -> None:
    """Each Auth0 failure mode maps to its specific exception subclass."""
    session = _build_session(status, body)
    auth = _new_auth(session)

    with pytest.raises(expected_exc):
        await auth.async_exchange_code("dummy_code", "dummy_verifier")


async def test_async_exchange_code_success_populates_tokens() -> None:
    """Happy path stores id_token, refresh_token, and expiry."""
    body = json.dumps(
        {
            "id_token": "eyJhbGciOi.eyJzdWIiOiJhdXRoMHwxMjMifQ.sig",
            "access_token": "access",
            "refresh_token": "refresh-stored",
            "expires_in": 86400,
        }
    )
    session = _build_session(200, body)
    auth = _new_auth(session)

    await auth.async_exchange_code("code", "verifier")

    assert auth.id_token == "eyJhbGciOi.eyJzdWIiOiJhdXRoMHwxMjMifQ.sig"
    assert auth.refresh_token == "refresh-stored"
    assert auth.is_authenticated is True
    # JWT sub claim is "auth0|123" — user_id strips the "auth0|" prefix.
    assert auth.user_id == "123"


async def test_async_exchange_code_sends_pkce_payload() -> None:
    """Token request must include code, code_verifier, and the correct grant type."""
    body = json.dumps({"id_token": "x.y.z", "expires_in": 60})
    session = _build_session(200, body)
    auth = _new_auth(session)

    await auth.async_exchange_code("AUTH_CODE", "MY_VERIFIER")

    _args, kwargs = session.post.call_args
    payload = kwargs["json"]
    assert payload["grant_type"] == "authorization_code"
    assert payload["code"] == "AUTH_CODE"
    assert payload["code_verifier"] == "MY_VERIFIER"
    assert payload["redirect_uri"].startswith("com.sharkninja.shark://")


# ---------------------------------------------------------------------------
# Refresh + headers + email
# ---------------------------------------------------------------------------


async def test_async_refresh_auth_uses_stored_refresh_token() -> None:
    """Refresh flow posts grant_type=refresh_token with the stored token."""
    body = json.dumps(
        {
            "id_token": "eyJhbGciOi.eyJzdWIiOiJhdXRoMHwxMjMifQ.sig",
            "refresh_token": "rotated-refresh",
            "expires_in": 86400,
        }
    )
    session = _build_session(200, body)
    auth = SharkAuth(
        websession=session,
        europe=False,
        refresh_token="seed-refresh",
        id_token=None,
        token_expiry=0,
    )

    await auth.async_refresh_auth()

    _args, kwargs = session.post.call_args
    payload = kwargs["json"]
    assert payload["grant_type"] == "refresh_token"
    assert payload["refresh_token"] == "seed-refresh"
    # Refresh tokens rotate — the new one must replace the seed.
    assert auth.refresh_token == "rotated-refresh"


async def test_async_refresh_auth_without_token_raises() -> None:
    """No refresh token means we have nothing to refresh; surface clearly."""
    session = _build_session(200, "{}")
    auth = _new_auth(session)
    with pytest.raises(SharkAuthError):
        await auth.async_refresh_auth()


async def test_token_request_sends_sharkclean_app_fingerprint() -> None:
    """Token requests carry SharkClean-app-style headers, not naked JSON.

    Auth0's anti-fraud is trained against the real Android app's request
    fingerprint. A POST that's missing User-Agent / Origin / Referer scores
    high enough to trip a ``requires_verification`` challenge even on an
    otherwise-clean account.
    """
    body = json.dumps({"id_token": "a.b.c", "refresh_token": "r", "expires_in": 60})
    session = _build_session(200, body)
    auth = _new_auth(session)

    await auth.async_exchange_code("c", "v")

    _args, kwargs = session.post.call_args
    headers = kwargs["headers"]
    assert headers["Content-Type"] == "application/json"
    assert "User-Agent" in headers
    assert "Mobile" in headers["User-Agent"] or "Android" in headers["User-Agent"]
    assert headers["Origin"] == "https://login.sharkninja.com"
    assert headers["Referer"] == "https://login.sharkninja.com/"


async def test_token_request_origin_follows_eu_host() -> None:
    """EU accounts hit ``logineu.sharkninja.com``; Origin/Referer must match."""
    body = json.dumps({"id_token": "a.b.c", "expires_in": 60})
    session = _build_session(200, body)
    auth = _new_auth(session, europe=True)

    await auth.async_exchange_code("c", "v")

    _args, kwargs = session.post.call_args
    assert kwargs["headers"]["Origin"] == "https://logineu.sharkninja.com"


def test_email_property_extracts_jwt_email_claim() -> None:
    """Email comes from the id_token's ``email`` claim, not the sub claim."""
    # JWT payload: {"sub": "auth0|123", "email": "person@example.com"}
    payload_b64 = (
        base64.urlsafe_b64encode(
            json.dumps({"sub": "auth0|123", "email": "person@example.com"}).encode()
        )
        .rstrip(b"=")
        .decode()
    )
    auth = SharkAuth(
        websession=MagicMock(spec=aiohttp.ClientSession),
        id_token=f"header.{payload_b64}.signature",
    )
    assert auth.email == "person@example.com"
    assert auth.user_id == "123"


def test_email_property_returns_none_without_claim() -> None:
    """Some Auth0 tenants omit the email claim — must not crash on it."""
    payload_b64 = (
        base64.urlsafe_b64encode(json.dumps({"sub": "auth0|123"}).encode())
        .rstrip(b"=")
        .decode()
    )
    auth = SharkAuth(
        websession=MagicMock(spec=aiohttp.ClientSession),
        id_token=f"header.{payload_b64}.signature",
    )
    assert auth.email is None


async def test_async_refresh_with_network_error_wrapped() -> None:
    """Transport-layer errors are wrapped as SharkAuthError, not bubbled raw."""
    session = MagicMock(spec=aiohttp.ClientSession)
    session.post = MagicMock(
        side_effect=aiohttp.ClientConnectorError(MagicMock(), OSError("boom"))
    )
    auth = SharkAuth(
        websession=session,
        refresh_token="r",
    )

    with pytest.raises(SharkAuthError) as exc_info:
        await auth.async_refresh_auth()
    # Specific subclasses are reserved for Auth0-tagged failures, so a generic
    # network error must surface as the base class — callers use that to
    # decide whether to retry vs. trigger reauth.
    assert type(exc_info.value) is SharkAuthError


# ---------------------------------------------------------------------------
# Account-blocked classifier (used by callers that catch errors from
# legacy code paths and need to decide whether to bubble to reauth.)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected_exc"),
    [
        (
            'Auth0 returned 401: {"error":"requires_verification"}',
            SharkAuthVerificationRequiredError,
        ),
        ("401: Suspicious request requires verification", SharkAuthVerificationRequiredError),
        ("Authentication failed: requires verification", SharkAuthVerificationRequiredError),
        ('{"error":"too_many_attempts"}', SharkAuthRateLimitedError),
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
        "Username or password incorrect",
        "Network is unreachable",
        "",
        "Email verification not required for this user",
    ],
)
def test_raise_if_auth0_account_blocked_passes_unrelated_errors(message: str) -> None:
    """No-op when the message doesn't match a known Auth0 account-level pattern."""
    raise_if_auth0_account_blocked(Exception(message))
