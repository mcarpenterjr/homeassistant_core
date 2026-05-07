"""Test the Shark IQ config flow."""

from unittest.mock import patch

import aiohttp
import pytest
from sharkiq import AylaApi, SharkIqAuthError, SharkIqError

from homeassistant import config_entries
from homeassistant.components.sharkiq.auth import (
    SharkAuthError,
    SharkAuthInvalidCredentialsError,
    SharkAuthRateLimitedError,
    SharkAuthVerificationRequiredError,
)
from homeassistant.components.sharkiq.const import DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from .const import (
    CONFIG,
    CONFIG_NO_REGION,
    TEST_PASSWORD,
    TEST_REGION,
    TEST_USERNAME,
    UNIQUE_ID,
)

from tests.common import MockConfigEntry


async def test_setup_success_no_region(hass: HomeAssistant) -> None:
    """Test reauth flow."""
    mock_config = MockConfigEntry(
        domain=DOMAIN, unique_id=UNIQUE_ID, data=CONFIG_NO_REGION
    )
    mock_config.add_to_hass(hass)

    with patch("homeassistant.components.sharkiq.async_setup_entry", return_value=True):
        result = await async_setup_component(hass=hass, domain=DOMAIN, config={})

    assert result is True


async def test_form(hass: HomeAssistant) -> None:
    """Test we get the form."""

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    with (
        patch(
            "homeassistant.components.sharkiq.config_flow.SharkAuth.async_sign_in",
            return_value=None,
        ),
        patch(
            "homeassistant.components.sharkiq.async_setup_entry",
            return_value=True,
        ) as mock_setup_entry,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            CONFIG,
        )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    assert result2["title"] == f"{TEST_USERNAME:s}"
    assert result2["data"] == {
        "username": TEST_USERNAME,
        "password": TEST_PASSWORD,
        "region": TEST_REGION,
    }

    await hass.async_block_till_done()
    mock_setup_entry.assert_called_once()


async def test_form_auth0_fails_ayla_succeeds(hass: HomeAssistant) -> None:
    """Test that Ayla fallback works when Auth0 direct auth fails."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with (
        patch(
            "homeassistant.components.sharkiq.config_flow.SharkAuth.async_sign_in",
            side_effect=SharkAuthError("Auth0 failed"),
        ),
        patch("sharkiq.AylaApi.async_sign_in", return_value=True),
        patch("sharkiq.AylaApi.async_set_cookie"),
        patch(
            "homeassistant.components.sharkiq.async_setup_entry",
            return_value=True,
        ) as mock_setup_entry,
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            CONFIG,
        )

    assert result2["type"] is FlowResultType.CREATE_ENTRY
    mock_setup_entry.assert_called_once()


@pytest.mark.parametrize(
    ("exc", "base_error"),
    [
        (SharkIqAuthError, "invalid_auth"),
        (aiohttp.ClientError, "cannot_connect"),
        (TypeError, "cannot_connect"),
        (SharkIqError, "unknown"),
    ],
)
async def test_form_error(hass: HomeAssistant, exc: Exception, base_error: str) -> None:
    """Test form errors."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with (
        patch(
            "homeassistant.components.sharkiq.config_flow.SharkAuth.async_sign_in",
            side_effect=SharkAuthError("Auth0 failed"),
        ),
        patch.object(AylaApi, "async_sign_in", side_effect=exc),
        patch("sharkiq.AylaApi.async_set_cookie"),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            CONFIG,
        )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"].get("base") == base_error


async def test_reauth_success(hass: HomeAssistant) -> None:
    """Test reauth flow."""
    mock_config = MockConfigEntry(domain=DOMAIN, unique_id=UNIQUE_ID, data=CONFIG)
    mock_config.add_to_hass(hass)

    result = await mock_config.start_reauth_flow(hass)

    with patch(
        "homeassistant.components.sharkiq.config_flow.SharkAuth.async_sign_in",
        return_value=None,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=CONFIG
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"


@pytest.mark.parametrize(
    ("side_effect", "result_type", "msg_field", "msg"),
    [
        (SharkIqAuthError, "form", "errors", "invalid_auth"),
        (aiohttp.ClientError, "abort", "reason", "cannot_connect"),
        (TypeError, "abort", "reason", "cannot_connect"),
        (SharkIqError, "abort", "reason", "unknown"),
    ],
)
async def test_reauth(
    hass: HomeAssistant,
    side_effect: Exception,
    result_type: str,
    msg_field: str,
    msg: str,
) -> None:
    """Test reauth failures."""
    mock_config = MockConfigEntry(domain=DOMAIN, unique_id=UNIQUE_ID, data=CONFIG)
    mock_config.add_to_hass(hass)

    result = await mock_config.start_reauth_flow(hass)

    with (
        patch(
            "homeassistant.components.sharkiq.config_flow.SharkAuth.async_sign_in",
            side_effect=SharkAuthError("Auth0 failed"),
        ),
        patch("sharkiq.AylaApi.async_sign_in", side_effect=side_effect),
        patch("sharkiq.AylaApi.async_set_cookie"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=CONFIG
        )
        msg_value = result[msg_field]
        if msg_field == "errors":
            msg_value = msg_value.get("base")

        assert result["type"] == result_type
        assert msg_value == msg


@pytest.mark.parametrize(
    ("auth_exc", "expected_error"),
    [
        (
            SharkAuthVerificationRequiredError("Verification required"),
            "auth0_verification",
        ),
        (
            SharkAuthRateLimitedError("Rate limited"),
            "auth0_rate_limited",
        ),
        (
            SharkAuthInvalidCredentialsError("Bad password"),
            "invalid_auth",
        ),
    ],
)
async def test_form_skegox_account_level_error_does_not_fall_through(
    hass: HomeAssistant,
    auth_exc: SharkAuthError,
    expected_error: str,
) -> None:
    """Account-level Auth0 errors must not retry via Ayla.

    Both backends share the same Auth0 tenant, so falling through after a
    requires_verification or too_many_attempts response would just keep
    deepening the rate-limit hole. We assert the error is surfaced AND that
    the Ayla path is never attempted.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    ayla_sign_in = patch("sharkiq.AylaApi.async_sign_in")
    with (
        patch(
            "homeassistant.components.sharkiq.config_flow.SharkAuth.async_sign_in",
            side_effect=auth_exc,
        ),
        ayla_sign_in as ayla_mock,
        patch("sharkiq.AylaApi.async_set_cookie"),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            CONFIG,
        )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"].get("base") == expected_error
    # Crucial: Ayla must not have been called. Otherwise we'd be re-hitting the
    # same Auth0 tenant and digging the rate-limit hole deeper.
    ayla_mock.assert_not_called()


@pytest.mark.parametrize(
    ("ayla_message", "expected_error"),
    [
        # The pip lib's password-grant path wraps Auth0's response body in a
        # SharkIqAuthError whose str(err) contains the underscore form.
        (
            "Auth0 password grant failed: 401 "
            '{"error":"requires_verification","error_description":"Suspicious"}',
            "auth0_verification",
        ),
        # The pip lib's legacy cookie path lets the unwrapped Auth0Error
        # message bubble through — different phrasing, same intent.
        (
            "401: Suspicious request requires verification",
            "auth0_verification",
        ),
        # 429 path from the pip lib's Ayla token_sign_in step.
        (
            "429: Your account has been blocked after multiple consecutive login attempts.",
            "auth0_rate_limited",
        ),
    ],
)
async def test_form_ayla_path_surfaces_auth0_account_errors(
    hass: HomeAssistant,
    ayla_message: str,
    expected_error: str,
) -> None:
    """Auth0 anti-fraud signals leaking through the Ayla wrapper get classified.

    Even though the Skegox path is preferred, accounts not yet migrated still
    hit Ayla — and Ayla bubbles the same Auth0 errors with different wrapping.
    Without this classification the user would see "invalid_auth" or "unknown"
    instead of the actionable "open the SharkClean app" message.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    with (
        patch(
            "homeassistant.components.sharkiq.config_flow.SharkAuth.async_sign_in",
            side_effect=SharkAuthError("Skegox unreachable"),
        ),
        patch(
            "sharkiq.AylaApi.async_sign_in",
            side_effect=SharkIqAuthError(ayla_message),
        ),
        patch("sharkiq.AylaApi.async_set_cookie"),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            CONFIG,
        )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"].get("base") == expected_error


async def test_form_ayla_path_unwrapped_auth0_error_classified(
    hass: HomeAssistant,
) -> None:
    """Unwrapped exceptions from the pip lib's cookie path still get classified.

    The pip lib's ``_legacy_cookie_sign_in`` doesn't always wrap upstream
    errors; ``auth0.exceptions.Auth0Error`` can leak through to our generic
    ``except Exception`` handler. Without message-sniffing in that branch the
    user would see ``unknown`` ("Open an issue on GitHub").
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Simulate the pip lib's behavior of letting the underlying Auth0Error
    # leak as a bare Exception subclass. We can't import Auth0Error reliably
    # (transitive dep) so use a generic Exception with the right message —
    # the classifier sniffs str(err), not the type.
    class FakeAuth0Error(Exception):
        """Stand-in for auth0.exceptions.Auth0Error to avoid importing the dep."""

    with (
        patch(
            "homeassistant.components.sharkiq.config_flow.SharkAuth.async_sign_in",
            side_effect=SharkAuthError("Skegox unreachable"),
        ),
        patch(
            "sharkiq.AylaApi.async_sign_in",
            side_effect=FakeAuth0Error("401: Suspicious request requires verification"),
        ),
        patch("sharkiq.AylaApi.async_set_cookie"),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            CONFIG,
        )

    assert result2["type"] is FlowResultType.FORM
    assert result2["errors"].get("base") == "auth0_verification"


async def test_reauth_stays_on_form_for_auth0_verification(
    hass: HomeAssistant,
) -> None:
    """Reauth keeps the user on the form for Auth0 anti-fraud errors.

    The user can clear the verification challenge externally (sign in via the
    SharkClean app) and then resubmit the form. Aborting the flow with the
    error reason would force them to start reauth again from scratch, with
    extra clicks for no information gained.
    """
    mock_config = MockConfigEntry(domain=DOMAIN, unique_id=UNIQUE_ID, data=CONFIG)
    mock_config.add_to_hass(hass)

    result = await mock_config.start_reauth_flow(hass)

    with patch(
        "homeassistant.components.sharkiq.config_flow.SharkAuth.async_sign_in",
        side_effect=SharkAuthVerificationRequiredError("Verification required"),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=CONFIG
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"].get("base") == "auth0_verification"
