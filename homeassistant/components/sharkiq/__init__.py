"""Shark IQ integration.

Authentication uses PKCE / authorization-code-with-paste against
SharkNinja's Auth0 tenant — see :mod:`auth` and the config flow for
details. Tokens are persisted on the config entry; setup refreshes
the access token if it's near expiry, and a refresh failure raises
:class:`ConfigEntryAuthFailed` to trigger a fresh PKCE flow.

Device data flows through the modern ``thor.skegox.com`` REST API
(:mod:`skegox`). Newer hybrid models (RV2500AX et al.) use a slightly
different shadow schema with value/timestamp wrappers and an ``items``
collection key — both are handled.

The integration polls because the skegox REST API has no push channel.
The coordinator's interval adapts to vacuum activity (30 s while
cleaning/returning, 15 minutes while docked) and entity availability
sticks for 15 minutes past the last successful refresh so transient
network failures don't flap the entity state.
"""

from __future__ import annotations

import asyncio
import socket

import aiohttp

from homeassistant import exceptions
from homeassistant.const import CONF_REGION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .auth import SharkAuth, SharkAuthError
from .const import (
    API_TIMEOUT,
    BACKEND_SKEGOX,
    CONF_BACKEND,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
    LOGGER,
    PLATFORMS,
    SHARKIQ_REGION_DEFAULT,
    SHARKIQ_REGION_EUROPE,
)
from .coordinator import SharkIqConfigEntry, SharkIqUpdateCoordinator
from .services import async_refresh_clean_room_schema, async_setup_services
from .skegox import SkegoxApi, SkegoxApiError

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the component."""
    async_setup_services(hass)
    return True


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: SharkIqConfigEntry
) -> bool:
    """Migrate older entries to PKCE-based auth.

    Version 1 entries stored ``username`` / ``password`` for the password
    grant flow. Auth0 anti-fraud now rejects that grant, so v2 only stores
    refresh + id tokens obtained via the PKCE flow. Strip credentials and
    let HA trigger reauth (which runs the new flow) on next setup.
    """
    if config_entry.version == 1:
        LOGGER.info(
            "Migrating Shark IQ config entry %s from v1 (password) to v2 (PKCE)",
            config_entry.entry_id,
        )
        new_data = {
            CONF_REGION: config_entry.data.get(CONF_REGION, SHARKIQ_REGION_DEFAULT),
        }
        if (backend := config_entry.data.get(CONF_BACKEND)) is not None:
            new_data[CONF_BACKEND] = backend
        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=2
        )
    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: SharkIqConfigEntry
) -> bool:
    """Initialize the sharkiq platform via config entry."""
    europe = config_entry.data.get(CONF_REGION) == SHARKIQ_REGION_EUROPE

    refresh_token = config_entry.data.get(CONF_REFRESH_TOKEN)
    if not refresh_token:
        # No tokens means a freshly-migrated v1 entry or one whose user
        # cleared them — either way, the user needs to do PKCE again.
        raise exceptions.ConfigEntryAuthFailed(
            "Shark IQ needs to be re-authorized via the SharkClean login flow"
        )

    websession = async_create_clientsession(
        hass,
        cookie_jar=aiohttp.CookieJar(unsafe=True, quote_cookie=False),
        family=socket.AF_INET,
    )

    auth = SharkAuth(
        websession=websession,
        europe=europe,
        refresh_token=refresh_token,
        id_token=config_entry.data.get(CONF_ID_TOKEN),
        token_expiry=config_entry.data.get(CONF_TOKEN_EXPIRY, 0),
    )

    # Refresh the access token whenever it's missing, expired, or within the
    # 10-minute "expiring soon" window. Refresh requests don't trigger
    # ``requires_verification`` so this is the steady-state path; a refresh
    # failure means the refresh token itself is no longer valid and we have
    # to fall back to a fresh PKCE flow via reauth.
    if not auth.is_authenticated or auth.token_expiring_soon:
        try:
            async with asyncio.timeout(API_TIMEOUT):
                await auth.async_refresh_auth()
        except SharkAuthError as err:
            LOGGER.warning("Shark IQ token refresh failed: %s", err)
            raise exceptions.ConfigEntryAuthFailed(str(err)) from err
        except TimeoutError as err:
            raise exceptions.ConfigEntryNotReady(
                "Timed out refreshing Shark IQ token"
            ) from err
        _persist_tokens(hass, config_entry, auth)

    skegox_api = SkegoxApi(auth, websession, europe=europe)

    try:
        async with asyncio.timeout(API_TIMEOUT):
            devices = await skegox_api.async_get_devices()
    except SharkAuthError as err:
        raise exceptions.ConfigEntryAuthFailed(str(err)) from err
    except (SkegoxApiError, TimeoutError, aiohttp.ClientError) as err:
        raise exceptions.ConfigEntryNotReady(
            f"Failed to load Shark IQ devices: {err}"
        ) from err

    if not devices:
        raise exceptions.ConfigEntryNotReady(
            "Skegox returned 0 devices for this account"
        )

    device_names = ", ".join(d.name for d in devices)
    LOGGER.info(
        "Found %d device(s) via skegox backend: %s", len(devices), device_names
    )

    if config_entry.data.get(CONF_BACKEND) != BACKEND_SKEGOX:
        hass.config_entries.async_update_entry(
            config_entry,
            data={**config_entry.data, CONF_BACKEND: BACKEND_SKEGOX},
        )

    coordinator = SharkIqUpdateCoordinator(
        hass,
        config_entry,
        shark_vacs=devices,
        skegox_api=skegox_api,
    )

    await coordinator.async_config_entry_first_refresh()

    config_entry.runtime_data = coordinator
    _async_remove_orphan_entities(hass, config_entry, coordinator)

    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Now that this entry's rooms are known, refresh the clean_room service
    # description so the automation editor's room field becomes a real
    # dropdown populated with this device's rooms.
    async_refresh_clean_room_schema(hass)

    return True


def _persist_tokens(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    auth: SharkAuth,
) -> None:
    """Write the latest tokens back to the config entry.

    Refresh-token rotation is on for SharkNinja's Auth0 tenant — every
    refresh response can include a new refresh_token that supersedes the
    one we used. Persist after each refresh so the next HA restart isn't
    holding a stale token that's about to be revoked.
    """
    if auth.refresh_token is None or auth.id_token is None:
        return
    new_data = {
        **config_entry.data,
        CONF_REFRESH_TOKEN: auth.refresh_token,
        CONF_ID_TOKEN: auth.id_token,
        CONF_TOKEN_EXPIRY: auth.token_expiry,
    }
    if new_data != dict(config_entry.data):
        hass.config_entries.async_update_entry(config_entry, data=new_data)


def _all_loaded_coordinators(
    hass: HomeAssistant,
) -> list[SharkIqUpdateCoordinator]:
    """Yield every loaded sharkiq coordinator across all config entries.

    Used by services.py to union room lists for the dynamic service schema
    without taking a hard import dependency on coordinator state.
    """
    result: list[SharkIqUpdateCoordinator] = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        coordinator = getattr(entry, "runtime_data", None)
        if isinstance(coordinator, SharkIqUpdateCoordinator):
            result.append(coordinator)
    return result


def _async_remove_orphan_entities(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    coordinator: SharkIqUpdateCoordinator,
) -> None:
    """Drop entity-registry rows for unique_ids we no longer create.

    Two flavors of orphan:

    Legacy UX iterations — the integration has gone through earlier room-
    cleaning composer designs that left behind unique_ids we never
    recreate:

    1. One ``button.<vac>_clean_<room>`` per room.
    2. Queue switches + clean-mode select + single start button.

    Renamed/removed rooms — per-room select switches use a unique_id of
    the form ``{serial}_select_<slug>``. When the user renames or merges a
    room in the SharkClean app the MARD signature changes and the old slug
    no longer maps to a current room. Without this cleanup, every rename
    leaves behind a dead switch that looks identical to the new one but
    does nothing.

    The exclusion list keeps current entities (``_preset_`` buttons, the
    ``_clean_selected`` button) safe from the legacy substring matches.
    """
    from homeassistant.util import slugify  # late import: top of-module cycle

    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    serials = set(coordinator.shark_vacs)

    # (entity_domain, unique_id_substring) — substring lookup keeps us safe
    # when prefix shapes shift.
    stale_substrings_by_domain: dict[str, tuple[str, ...]] = {
        "button": ("_clean_", "_start_room_clean"),
        "select": ("_clean_type",),
        "switch": ("_queue_",),
    }
    # Current-design substrings to protect from the legacy matchers above.
    # ``_clean_selected`` is intentionally NOT here: a previous iteration
    # shipped a SharkCleanSelectedRoomsButton with that suffix and the
    # current design (selection-aware Start) doesn't recreate it. Letting
    # it match the legacy ``_clean_`` substring drops the orphan cleanly.
    current_substrings = ("_preset_", "_select_")

    # Build the current set of valid select-switch unique_ids for the
    # rename/merge orphan check.
    valid_select_unique_ids: set[str] = set()
    for device in coordinator.shark_vacs.values():
        display_rooms = getattr(device, "display_rooms", None) or {}
        for room_name in display_rooms:
            valid_select_unique_ids.add(
                f"{device.serial_number}_select_{slugify(room_name)}"
            )

    for entry in entries:
        unique_id = entry.unique_id or ""

        # Renamed/removed room cleanup: any select switch whose unique_id
        # isn't in the current valid set.
        if entry.domain == "switch" and "_select_" in unique_id:
            if unique_id not in valid_select_unique_ids:
                LOGGER.debug(
                    "Removing orphan select switch %s (%s)",
                    entry.entity_id,
                    unique_id,
                )
                registry.async_remove(entry.entity_id)
            continue

        # Legacy UX cleanup.
        substrings = stale_substrings_by_domain.get(entry.domain)
        if substrings is None:
            continue
        if any(s in unique_id for s in current_substrings):
            continue
        for serial in serials:
            if not unique_id.startswith(serial):
                continue
            if any(s in unique_id for s in substrings):
                LOGGER.debug(
                    "Removing orphan sharkiq entity %s (%s)",
                    entry.entity_id,
                    unique_id,
                )
                registry.async_remove(entry.entity_id)
                break


async def async_unload_entry(
    hass: HomeAssistant, config_entry: SharkIqConfigEntry
) -> bool:
    """Unload a sharkiq config entry."""
    return await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
