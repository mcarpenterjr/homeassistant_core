"""Test the Shark IQ PKCE config flow."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from homeassistant import config_entries
from homeassistant.components.sharkiq.auth import SharkAuthError
from homeassistant.components.sharkiq.const import (
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
)
from homeassistant.const import CONF_REGION
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .const import TEST_REGION, UNIQUE_ID

from tests.common import MockConfigEntry


def _patch_exchange_success(
    *,
    user_id: str = "auth0|user123",
    email: str | None = "person@example.com",
    refresh_token: str = "rt-xyz",
    id_token: str = "id-token-xyz",
    token_expiry: float = 1_700_000_000.0,
):
    """Patch SharkAuth so async_exchange_code succeeds with predictable tokens.

    Avoids real HTTP calls in the config-flow tests; the auth module's own
    error-classification behavior is covered by ``test_auth.py``.
    """
    async def _stub_exchange(self, code, code_verifier):
        # Mutate internal state to mimic a successful token exchange.
        self._refresh_token = refresh_token
        self._id_token = id_token
        self._token_expiry = token_expiry

    return patch.multiple(
        "homeassistant.components.sharkiq.config_flow.SharkAuth",
        async_exchange_code=_stub_exchange,
        # Override the JWT-decoding properties so we don't have to mint a
        # real id_token — the tests care about the flow, not JWT shapes.
        user_id=property(lambda self: user_id.split("|", 1)[1] if "|" in user_id else user_id),
        email=property(lambda self: email),
    )


async def test_form_user_collects_region_then_shows_authorize(
    hass: HomeAssistant,
) -> None:
    """First step asks for region, then transitions to the authorize step."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result2 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: TEST_REGION}
    )

    assert result2["type"] is FlowResultType.FORM
    assert result2["step_id"] == "authorize"
    # The form should hand the user a real auth URL via placeholders so
    # they can copy it into a browser.
    placeholders = result2.get("description_placeholders") or {}
    assert "auth_url" in placeholders
    assert placeholders["auth_url"].startswith("https://login.sharkninja.com/authorize")


async def test_form_full_flow_creates_entry(hass: HomeAssistant) -> None:
    """Region → authorize → paste code → token exchange → entry created."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: TEST_REGION}
    )

    with (
        _patch_exchange_success(),
        patch(
            "homeassistant.components.sharkiq.async_setup_entry",
            return_value=True,
        ) as mock_setup_entry,
    ):
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect": "AUTH_CODE_VALUE"}
        )

    assert result3["type"] is FlowResultType.CREATE_ENTRY
    # Title prefers the email claim from the JWT for readability.
    assert result3["title"] == "person@example.com"
    # The crucial assertion: tokens are persisted, no username/password.
    assert result3["data"][CONF_REFRESH_TOKEN] == "rt-xyz"
    assert result3["data"][CONF_ID_TOKEN] == "id-token-xyz"
    assert result3["data"][CONF_TOKEN_EXPIRY] == 1_700_000_000.0
    assert result3["data"][CONF_REGION] == TEST_REGION
    assert "username" not in result3["data"]
    assert "password" not in result3["data"]
    mock_setup_entry.assert_called_once()


async def test_form_accepts_full_redirect_url(hass: HomeAssistant) -> None:
    """Pasting the full ``com.sharkninja.shark://...?code=...&state=...`` URL works.

    The browser fails to launch the iOS scheme but the URL bar still
    contains the params — pasting the whole string is the path of least
    resistance for the user.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: TEST_REGION}
    )
    # Inject a known PKCE state into the flow handler so we can paste a
    # matching state and confirm validation passes.
    flow = next(
        f for f in hass.config_entries.flow.async_progress() if f["flow_id"] == result["flow_id"]
    )
    handler = hass.config_entries.flow._progress[flow["flow_id"]]
    pasted_state = handler._pkce_state
    full_url = (
        "com.sharkninja.shark://login.sharkninja.com/ios/com.sharkninja.shark/callback"
        f"?code=THE_CODE&state={pasted_state}"
    )

    with (
        _patch_exchange_success(),
        patch(
            "homeassistant.components.sharkiq.async_setup_entry",
            return_value=True,
        ),
    ):
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect": full_url}
        )

    assert result3["type"] is FlowResultType.CREATE_ENTRY


async def test_form_rejects_state_mismatch(hass: HomeAssistant) -> None:
    """A redirect URL whose state doesn't match the flow's must be rejected.

    Defends against a user pasting an old or cross-flow redirect — without
    state validation we'd accept a code that wasn't ours.
    """
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: TEST_REGION}
    )

    bad = "com.sharkninja.shark://callback?code=X&state=this-is-not-the-real-state"

    with _patch_exchange_success():
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect": bad}
        )

    assert result3["type"] is FlowResultType.FORM
    assert result3["errors"]["base"] == "invalid_state"


async def test_form_rejects_input_without_code(hass: HomeAssistant) -> None:
    """Empty / non-code input surfaces ``invalid_redirect`` so the user can retry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: TEST_REGION}
    )

    result3 = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"redirect": "https://example.com/?error=access_denied"}
    )

    assert result3["type"] is FlowResultType.FORM
    assert result3["errors"]["base"] == "invalid_redirect"


@pytest.mark.parametrize(
    ("exc", "expected_error"),
    [
        (SharkAuthError("token exchange failed"), "invalid_auth"),
        (TimeoutError("slow"), "cannot_connect"),
    ],
)
async def test_form_token_exchange_failures_show_inline_errors(
    hass: HomeAssistant,
    exc: Exception,
    expected_error: str,
) -> None:
    """Failed token exchange keeps the user on the form with a clear error key."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_REGION: TEST_REGION}
    )

    with patch(
        "homeassistant.components.sharkiq.config_flow.SharkAuth.async_exchange_code",
        side_effect=exc,
    ):
        result3 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect": "AUTH_CODE_VALUE"}
        )

    assert result3["type"] is FlowResultType.FORM
    assert result3["errors"]["base"] == expected_error


async def test_reauth_uses_pkce_flow_and_updates_existing_entry(
    hass: HomeAssistant,
) -> None:
    """Reauth runs the same PKCE flow and merges new tokens into the existing entry."""
    mock_config = MockConfigEntry(
        domain=DOMAIN,
        unique_id=UNIQUE_ID,
        version=2,
        data={
            CONF_REGION: TEST_REGION,
            CONF_REFRESH_TOKEN: "old-refresh",
            CONF_ID_TOKEN: "old-id",
            CONF_TOKEN_EXPIRY: 0.0,
        },
    )
    mock_config.add_to_hass(hass)

    result = await mock_config.start_reauth_flow(hass)
    # Reauth jumps straight to the authorize step (no region prompt; we
    # already know the region from the existing entry).
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "authorize"

    with (
        _patch_exchange_success(refresh_token="rt-new", id_token="id-new"),
        patch(
            "homeassistant.components.sharkiq.async_setup_entry",
            return_value=True,
        ),
    ):
        result2 = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"redirect": "FRESH_AUTH_CODE"}
        )

    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "reauth_successful"
    # Entry should now hold the new tokens, not the old ones.
    assert mock_config.data[CONF_REFRESH_TOKEN] == "rt-new"
    assert mock_config.data[CONF_ID_TOKEN] == "id-new"
