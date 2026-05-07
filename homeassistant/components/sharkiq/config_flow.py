"""Config flow for Shark IQ integration.

Auth uses PKCE / authorization-code-with-paste:

1. ``async_step_user`` collects the region and generates a PKCE pair + state.
2. ``async_step_authorize`` shows the user the Auth0 URL to open in their
   browser. They log in (and complete any verification challenge interactively),
   then paste the resulting redirect URL — Auth0's redirect goes to a
   custom iOS app scheme the browser can't launch, but the URL bar still
   contains ``?code=…``.
3. We exchange the code at ``/oauth/token`` for refresh + id tokens and
   persist them on the config entry. From then on, ``grant_type=refresh_token``
   keeps things current — no further user interaction required.

This is the only flow that reliably bypasses Auth0's ``requires_verification``
challenge against SharkNinja's tenant; the password-realm grant gets server-IP
fingerprinted and rejected.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import secrets
import socket
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_REGION
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .auth import (
    SharkAuth,
    SharkAuthError,
    build_authorize_url,
    generate_pkce_pair,
    parse_callback_input,
)
from .const import (
    CLEAN_TYPES,
    CONF_ID_TOKEN,
    CONF_PRESETS,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
    FAN_SPEED_NAMES,
    LOGGER,
    PRESET_CLEAN_TYPE,
    PRESET_FAN_SPEED,
    PRESET_ID,
    PRESET_NAME,
    PRESET_ROOMS,
    PRESET_SERIAL,
    SHARKIQ_REGION_DEFAULT,
    SHARKIQ_REGION_EUROPE,
    SHARKIQ_REGION_OPTIONS,
)

CONF_REDIRECT_INPUT = "redirect"

SHARKIQ_REGION_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_REGION, default=SHARKIQ_REGION_DEFAULT
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=SHARKIQ_REGION_OPTIONS, translation_key="region"
            ),
        ),
    }
)

SHARKIQ_REDIRECT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_REDIRECT_INPUT): str,
    }
)


class SharkIqConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Shark IQ."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the flow's PKCE state."""
        self._region: str = SHARKIQ_REGION_DEFAULT
        self._europe: bool = False
        self._pkce_verifier: str = ""
        self._pkce_challenge: str = ""
        self._pkce_state: str = ""
        # Set during reauth so we update the existing entry instead of
        # creating a new one.
        self._reauth_entry: ConfigEntry | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> SharkIqOptionsFlow:
        """Return the options flow for managing cleaning presets."""
        return SharkIqOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the region, then transition to authorization."""
        if user_input is not None:
            self._region = user_input[CONF_REGION]
            self._europe = self._region == SHARKIQ_REGION_EUROPE
            self._pkce_verifier, self._pkce_challenge = generate_pkce_pair()
            self._pkce_state = secrets.token_urlsafe(32)
            return await self.async_step_authorize()

        return self.async_show_form(
            step_id="user",
            data_schema=SHARKIQ_REGION_SCHEMA,
        )

    async def async_step_authorize(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the Auth0 URL and exchange the pasted callback for tokens."""
        errors: dict[str, str] = {}
        auth_url = build_authorize_url(
            europe=self._europe,
            code_challenge=self._pkce_challenge,
            state=self._pkce_state,
        )

        if user_input is not None:
            try:
                code, state = parse_callback_input(user_input[CONF_REDIRECT_INPUT])
            except ValueError:
                errors["base"] = "invalid_redirect"
            else:
                # If the redirect URL contained a state, verify it matches what
                # we generated to defend against the user pasting an old or
                # cross-flow redirect. A bare ``code`` without state can't be
                # verified, but the user has to actively paste it so the
                # CSRF risk is minimal.
                if state is not None and state != self._pkce_state:
                    errors["base"] = "invalid_state"
                else:
                    info, exchange_errors = await self._async_exchange_for_tokens(code)
                    if info is not None:
                        return await self._async_finalize_entry(info)
                    errors.update(exchange_errors)

        return self.async_show_form(
            step_id="authorize",
            data_schema=SHARKIQ_REDIRECT_SCHEMA,
            description_placeholders={"auth_url": auth_url},
            errors=errors,
        )

    async def _async_exchange_for_tokens(
        self, code: str
    ) -> tuple[dict[str, Any] | None, dict[str, str]]:
        """Exchange a PKCE authorization code for tokens.

        Returns ``(info, errors)``. ``info`` carries the entry payload
        when successful; on failure it is None and ``errors`` populates
        the form's error map.
        """
        websession = async_create_clientsession(
            self.hass,
            cookie_jar=aiohttp.CookieJar(unsafe=True, quote_cookie=False),
            family=socket.AF_INET,
        )
        auth = SharkAuth(websession=websession, europe=self._europe)
        try:
            async with asyncio.timeout(15):
                await auth.async_exchange_code(code, self._pkce_verifier)
        except TimeoutError:
            LOGGER.debug("PKCE token exchange timed out")
            return None, {"base": "cannot_connect"}
        except SharkAuthError as err:
            LOGGER.error("PKCE token exchange failed: %s", err)
            return None, {"base": "invalid_auth"}

        if not auth.refresh_token or not auth.id_token:
            LOGGER.error("PKCE token exchange returned no tokens")
            return None, {"base": "unknown"}

        info: dict[str, Any] = {
            "user_id": auth.user_id,
            "title": auth.email or auth.user_id or "Shark IQ",
            "data": {
                CONF_REGION: self._region,
                CONF_REFRESH_TOKEN: auth.refresh_token,
                CONF_ID_TOKEN: auth.id_token,
                CONF_TOKEN_EXPIRY: auth.token_expiry,
            },
        }
        return info, {}

    async def _async_finalize_entry(
        self, info: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create or update the config entry with newly-acquired tokens."""
        if self._reauth_entry is not None:
            # Preserve fields the reauth flow shouldn't touch (backend
            # selection, options) by merging into existing data.
            updated_data = {**self._reauth_entry.data, **info["data"]}
            self.hass.config_entries.async_update_entry(
                self._reauth_entry, data=updated_data
            )
            await self.hass.config_entries.async_reload(self._reauth_entry.entry_id)
            return self.async_abort(reason="reauth_successful")

        # New entry. Use Auth0 ``sub`` as unique_id — stable across email
        # changes and never empty for a successful exchange.
        if info["user_id"]:
            await self.async_set_unique_id(info["user_id"])
            self._abort_if_unique_id_configured()
        return self.async_create_entry(title=info["title"], data=info["data"])

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Trigger reauth — same PKCE flow as initial setup."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        if self._reauth_entry is not None:
            self._region = self._reauth_entry.data.get(
                CONF_REGION, SHARKIQ_REGION_DEFAULT
            )
            self._europe = self._region == SHARKIQ_REGION_EUROPE
        self._pkce_verifier, self._pkce_challenge = generate_pkce_pair()
        self._pkce_state = secrets.token_urlsafe(32)
        return await self.async_step_authorize()




class SharkIqOptionsFlow(OptionsFlowWithReload):
    """Manage cleaning presets for the SharkIQ integration.

    Each preset bundles a name, the target vacuum's serial number, the
    rooms to clean, the clean mode, and the fan speed. Presets are stored
    as a list under ``config_entry.options[CONF_PRESETS]``; one button
    entity is created per preset on entry setup.
    """

    def __init__(self) -> None:
        """Set up scratch state for in-progress edits."""
        self._target_serial: str | None = None
        self._editing_id: str | None = None

    @property
    def _presets(self) -> list[dict[str, Any]]:
        """Return the current preset list (a copy)."""
        return list(self.config_entry.options.get(CONF_PRESETS, []))

    def _device_choices(self) -> dict[str, str]:
        """Return ``{serial_number: display_name}`` for known vacuums."""
        coordinator = self.config_entry.runtime_data
        return {vac.serial_number: vac.name for vac in coordinator.shark_vacs.values()}

    def _device_rooms(self, serial: str) -> list[str]:
        """Return the named rooms reported by the given vacuum, or ``[]``."""
        coordinator = self.config_entry.runtime_data
        device = coordinator.shark_vacs.get(serial)
        if device is None:
            return []
        try:
            room_list = device.get_property_value("Robot_Room_List")
        except KeyError:
            return []
        if not room_list or ":" not in room_list:
            return []
        return room_list.split(":")[1:]

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Top-level menu: add a preset, edit / remove an existing one."""
        menu_options: list[str] = ["add_preset"]
        if self._presets:
            menu_options.extend(["edit_preset", "remove_preset"])
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_add_preset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick the target vacuum (auto-skipped on single-vacuum accounts)."""
        choices = self._device_choices()
        if not choices:
            return self.async_abort(reason="no_devices")

        if len(choices) == 1:
            self._target_serial = next(iter(choices))
            self._editing_id = None
            return await self.async_step_preset_form()

        if user_input is not None:
            self._target_serial = user_input[PRESET_SERIAL]
            self._editing_id = None
            return await self.async_step_preset_form()

        return self.async_show_form(
            step_id="add_preset",
            data_schema=vol.Schema(
                {
                    vol.Required(PRESET_SERIAL): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(value=serial, label=name)
                                for serial, name in choices.items()
                            ],
                        )
                    ),
                }
            ),
        )

    async def async_step_preset_form(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect preset name / rooms / clean type / fan speed."""
        assert self._target_serial is not None
        rooms = self._device_rooms(self._target_serial)
        existing = next(
            (p for p in self._presets if p.get(PRESET_ID) == self._editing_id),
            None,
        )

        if user_input is not None:
            new_preset: dict[str, Any] = {
                PRESET_ID: existing.get(PRESET_ID) if existing else secrets.token_hex(4),
                PRESET_SERIAL: self._target_serial,
                PRESET_NAME: user_input[PRESET_NAME],
                PRESET_ROOMS: user_input[PRESET_ROOMS],
                PRESET_CLEAN_TYPE: user_input[PRESET_CLEAN_TYPE],
                PRESET_FAN_SPEED: user_input[PRESET_FAN_SPEED],
            }
            updated = [p for p in self._presets if p.get(PRESET_ID) != new_preset[PRESET_ID]]
            updated.append(new_preset)
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, CONF_PRESETS: updated},
            )

        defaults_name = existing.get(PRESET_NAME, "") if existing else ""
        defaults_rooms = existing.get(PRESET_ROOMS, []) if existing else []
        defaults_clean = existing.get(PRESET_CLEAN_TYPE, "dry") if existing else "dry"
        defaults_fan = existing.get(PRESET_FAN_SPEED, "Normal") if existing else "Normal"

        # Use a multi-select with custom_value so the user can type a room
        # name that isn't in the discovered list (e.g. when the device hasn't
        # yet reported its room map, or when a room name has been renamed in
        # the SharkClean app between polls).
        schema = vol.Schema(
            {
                vol.Required(PRESET_NAME, default=defaults_name): str,
                vol.Required(
                    PRESET_ROOMS, default=defaults_rooms
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=rooms,
                        multiple=True,
                        custom_value=True,
                    )
                ),
                vol.Required(
                    PRESET_CLEAN_TYPE, default=defaults_clean
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=list(CLEAN_TYPES),
                        translation_key="clean_type",
                    )
                ),
                vol.Required(
                    PRESET_FAN_SPEED, default=defaults_fan
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(options=list(FAN_SPEED_NAMES))
                ),
            }
        )

        return self.async_show_form(step_id="preset_form", data_schema=schema)

    async def async_step_edit_preset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which existing preset to edit, then route to the form."""
        presets = self._presets
        if not presets:
            return await self.async_step_init()

        if user_input is not None:
            chosen = next(
                (p for p in presets if p.get(PRESET_ID) == user_input[PRESET_ID]),
                None,
            )
            if chosen is None:
                return await self.async_step_init()
            self._target_serial = chosen[PRESET_SERIAL]
            self._editing_id = chosen[PRESET_ID]
            return await self.async_step_preset_form()

        choices = self._device_choices()
        return self.async_show_form(
            step_id="edit_preset",
            data_schema=vol.Schema(
                {
                    vol.Required(PRESET_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=p[PRESET_ID],
                                    label=(
                                        f"{p[PRESET_NAME]}"
                                        f" ({choices.get(p[PRESET_SERIAL], p[PRESET_SERIAL])})"
                                    ),
                                )
                                for p in presets
                            ],
                        )
                    ),
                }
            ),
        )

    async def async_step_remove_preset(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which preset(s) to delete."""
        presets = self._presets
        if not presets:
            return await self.async_step_init()

        if user_input is not None:
            kept = [p for p in presets if p[PRESET_ID] not in user_input[PRESET_ID]]
            return self.async_create_entry(
                title="",
                data={**self.config_entry.options, CONF_PRESETS: kept},
            )

        choices = self._device_choices()
        return self.async_show_form(
            step_id="remove_preset",
            data_schema=vol.Schema(
                {
                    vol.Required(PRESET_ID): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            multiple=True,
                            options=[
                                selector.SelectOptionDict(
                                    value=p[PRESET_ID],
                                    label=(
                                        f"{p[PRESET_NAME]}"
                                        f" ({choices.get(p[PRESET_SERIAL], p[PRESET_SERIAL])})"
                                    ),
                                )
                                for p in presets
                            ],
                        )
                    ),
                }
            ),
        )
