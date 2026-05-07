"""Config flow for Shark IQ integration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import secrets
import socket
from typing import Any

import aiohttp
from sharkiq import SharkIqAuthError, get_ayla_api
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_REGION, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .auth import (
    SharkAuth,
    SharkAuthError,
    SharkAuthInvalidCredentialsError,
    SharkAuthRateLimitedError,
    SharkAuthVerificationRequiredError,
    raise_if_auth0_account_blocked,
)
from .const import (
    CLEAN_TYPES,
    CONF_PRESETS,
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

SHARKIQ_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
        vol.Required(
            CONF_REGION, default=SHARKIQ_REGION_DEFAULT
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=SHARKIQ_REGION_OPTIONS, translation_key="region"
            ),
        ),
    }
)


async def _validate_input(
    hass: HomeAssistant, data: Mapping[str, Any]
) -> dict[str, str]:
    """Validate the user input allows us to connect.

    Tries Auth0 direct auth first (for skegox), falls back to sharkiq library (Ayla).
    """
    europe = data.get(CONF_REGION) == SHARKIQ_REGION_EUROPE
    websession = async_create_clientsession(
        hass,
        cookie_jar=aiohttp.CookieJar(unsafe=True, quote_cookie=False),
        family=socket.AF_INET,
    )

    # Try Auth0 direct authentication first (works for both backends)
    try:
        async with asyncio.timeout(15):
            auth = SharkAuth(
                username=data[CONF_USERNAME],
                password=data[CONF_PASSWORD],
                websession=websession,
                europe=europe,
            )
            await auth.async_sign_in()
            LOGGER.debug("Auth0 direct authentication successful")
            return {"title": data[CONF_USERNAME]}
    except SharkAuthVerificationRequiredError as err:
        # Both backends share the Auth0 tenant, so falling through to Ayla
        # would just deepen the rate-limit hole. Surface the actionable error.
        raise Auth0VerificationRequired(str(err)) from err
    except SharkAuthRateLimitedError as err:
        raise Auth0RateLimited(str(err)) from err
    except SharkAuthInvalidCredentialsError as err:
        raise InvalidAuth(str(err)) from err
    except SharkAuthError as err:
        LOGGER.debug("Auth0 direct auth failed: %s, trying Ayla fallback", err)
    except TimeoutError:
        LOGGER.debug("Auth0 direct auth timed out, trying Ayla fallback")

    # Fall back to sharkiq library's auth (legacy Ayla path)
    ayla_api = get_ayla_api(
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        websession=websession,
        europe=europe,
    )

    try:
        async with asyncio.timeout(15):
            LOGGER.debug("Initialize connection to Ayla networks API")
            await ayla_api.async_sign_in()
    except (TimeoutError, aiohttp.ClientError, TypeError) as error:
        LOGGER.error(error)
        raise CannotConnect(
            "Unable to connect to SharkIQ services. Check your region settings."
        ) from error
    except SharkIqAuthError as error:
        # The pip lib's SharkIqAuthError wraps Auth0 anti-fraud signals; check
        # the message for known patterns before defaulting to invalid_auth.
        try:
            raise_if_auth0_account_blocked(error)
        except SharkAuthVerificationRequiredError as err:
            raise Auth0VerificationRequired(str(err)) from err
        except SharkAuthRateLimitedError as err:
            raise Auth0RateLimited(str(err)) from err
        LOGGER.error(error)
        raise InvalidAuth(
            "Username or password incorrect. Please check your credentials."
        ) from error
    except Exception as error:
        # The pip lib leaks unwrapped auth0.exceptions.Auth0Error on its
        # cookie-fallback path; same message-sniffing handles it.
        try:
            raise_if_auth0_account_blocked(error)
        except SharkAuthVerificationRequiredError as err:
            raise Auth0VerificationRequired(str(err)) from err
        except SharkAuthRateLimitedError as err:
            raise Auth0RateLimited(str(err)) from err
        LOGGER.exception("Unexpected exception")
        LOGGER.error(error)
        raise UnknownAuth(
            "An unknown error occurred. Check your region settings and open an issue on Github if the issue persists."
        ) from error

    # Return info that you want to store in the config entry.
    return {"title": data[CONF_USERNAME]}


class SharkIqConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Shark IQ."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> SharkIqOptionsFlow:
        """Return the options flow for managing cleaning presets."""
        return SharkIqOptionsFlow()

    async def _async_validate_input(
        self, user_input: Mapping[str, Any]
    ) -> tuple[dict[str, str] | None, dict[str, str]]:
        """Validate form input."""
        errors = {}
        info = None

        # noinspection PyBroadException
        try:
            info = await _validate_input(self.hass, user_input)
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except Auth0VerificationRequired:
            errors["base"] = "auth0_verification"
        except Auth0RateLimited:
            errors["base"] = "auth0_rate_limited"
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except UnknownAuth:
            errors["base"] = "unknown"
        return info, errors

    async def async_step_user(
        self, user_input: dict[str, str] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            info, errors = await self._async_validate_input(user_input)
            if info:
                await self.async_set_unique_id(user_input[CONF_USERNAME])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=SHARKIQ_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-auth if login is invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initiated by reauthentication."""
        errors: dict[str, str] = {}

        if user_input is not None:
            _, errors = await self._async_validate_input(user_input)

            if not errors:
                errors = {"base": "unknown"}
                if entry := await self.async_set_unique_id(self.unique_id):
                    self.hass.config_entries.async_update_entry(entry, data=user_input)
                    return self.async_abort(reason="reauth_successful")

            # Stay on the form for retryable errors so the user can fix the
            # underlying issue (clear the Auth0 verification, fix credentials,
            # wait out the rate limit) and submit again. Only abort on
            # terminal failures.
            retryable = {"invalid_auth", "auth0_verification", "auth0_rate_limited"}
            if errors["base"] not in retryable:
                return self.async_abort(reason=errors["base"])

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=SHARKIQ_SCHEMA,
            errors=errors,
        )


class CannotConnect(HomeAssistantError):
    """Error to indicate we cannot connect."""


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class UnknownAuth(HomeAssistantError):
    """Error to indicate there is an uncaught auth error."""


class Auth0VerificationRequired(HomeAssistantError):
    """Auth0 anti-fraud has flagged this account/IP and requires interactive verification."""


class Auth0RateLimited(HomeAssistantError):
    """Auth0 has temporarily blocked the account after too many failed attempts."""


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
