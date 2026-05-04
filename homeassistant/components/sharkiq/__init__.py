"""Shark IQ integration.

Supports both backends SharkNinja has used:

- **Skegox** (``thor.skegox.com``): the current backend. Auth via Auth0
  password grant; device data via a JSON shadow API. Implemented in
  :mod:`auth` and :mod:`skegox`. Newer hybrid models (RV2500AX et al.)
  use a slightly different shadow schema with value/timestamp wrappers
  and an ``items`` collection key — both are handled.
- **Ayla Networks**: the legacy backend used before SharkNinja's API
  migration. Wraps the upstream ``sharkiq`` PyPI library.

``async_setup_entry`` tries Skegox first (covers freshly-migrated
accounts which return 0 devices on Ayla), falls back to Ayla if the
account hasn't been migrated, and remembers the working backend in
``config_entry.data["backend"]`` so subsequent loads skip straight to it.

The integration polls because the skegox REST API has no push channel.
The coordinator's interval adapts to vacuum activity (30 s while
cleaning/returning, 15 minutes while docked) and entity availability
sticks for 15 minutes past the last successful refresh so transient
network failures don't flap the entity state.
"""

import asyncio
from contextlib import suppress
import socket

import aiohttp
from sharkiq import (
    AylaApi,
    SharkIqAuthError,
    SharkIqAuthExpiringError,
    SharkIqNotAuthedError,
    get_ayla_api,
)

from homeassistant import exceptions
from homeassistant.const import CONF_PASSWORD, CONF_REGION, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.typing import ConfigType

from .auth import SharkAuth, SharkAuthError
from .const import (
    API_TIMEOUT,
    BACKEND_AYLA,
    BACKEND_SKEGOX,
    CONF_BACKEND,
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


class CannotConnect(exceptions.HomeAssistantError):
    """Error to indicate we cannot connect."""


async def async_connect_or_timeout(ayla_api: AylaApi) -> bool:
    """Connect to vacuum via Ayla."""
    try:
        async with asyncio.timeout(API_TIMEOUT):
            LOGGER.debug("Initialize connection to Ayla networks API")
            await ayla_api.async_sign_in()
    except SharkIqAuthError:
        LOGGER.error("Authentication error connecting to Shark IQ api")
        return False
    except TimeoutError as exc:
        LOGGER.error("Timeout expired")
        raise CannotConnect from exc
    except (aiohttp.ClientError, OSError) as exc:
        LOGGER.debug("Network error connecting to Ayla: %s", exc)
        raise CannotConnect from exc

    return True


async def _async_setup_skegox(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    websession: aiohttp.ClientSession,
    europe: bool,
) -> SharkIqUpdateCoordinator | None:
    """Try to set up via the skegox backend."""
    auth = SharkAuth(
        username=config_entry.data[CONF_USERNAME],
        password=config_entry.data[CONF_PASSWORD],
        websession=websession,
        europe=europe,
    )

    try:
        async with asyncio.timeout(API_TIMEOUT):
            await auth.async_sign_in()
    except (SharkAuthError, TimeoutError) as err:
        LOGGER.debug("Skegox auth failed: %s", err)
        return None

    skegox_api = SkegoxApi(auth, websession, europe=europe)

    try:
        async with asyncio.timeout(API_TIMEOUT):
            devices = await skegox_api.async_get_devices()
    except (SkegoxApiError, SharkAuthError, TimeoutError) as err:
        LOGGER.debug("Skegox device discovery failed: %s", err)
        return None

    if not devices:
        LOGGER.debug("Skegox returned 0 devices")
        return None

    device_names = ", ".join(d.name for d in devices)
    LOGGER.info(
        "Found %d device(s) via skegox backend: %s", len(devices), device_names
    )

    return SharkIqUpdateCoordinator(
        hass,
        config_entry,
        shark_vacs=devices,
        skegox_api=skegox_api,
    )


async def _async_setup_ayla(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    websession: aiohttp.ClientSession,
    europe: bool,
) -> SharkIqUpdateCoordinator | None:
    """Try to set up via the Ayla backend."""
    ayla_api = get_ayla_api(
        username=config_entry.data[CONF_USERNAME],
        password=config_entry.data[CONF_PASSWORD],
        websession=websession,
        europe=europe,
    )

    try:
        if not await async_connect_or_timeout(ayla_api):
            return None
    except CannotConnect:
        return None

    try:
        async with asyncio.timeout(API_TIMEOUT):
            shark_vacs = await ayla_api.async_get_devices(False)
    except (SharkIqAuthError, aiohttp.ClientError, TimeoutError):
        LOGGER.debug("Ayla device discovery failed", exc_info=True)
        return None

    if not shark_vacs:
        LOGGER.debug("Ayla returned 0 devices")
        return None

    device_names = ", ".join(d.name for d in shark_vacs)
    LOGGER.info(
        "Found %d device(s) via Ayla backend: %s", len(shark_vacs), device_names
    )

    return SharkIqUpdateCoordinator(
        hass,
        config_entry,
        shark_vacs=shark_vacs,
        ayla_api=ayla_api,
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the component."""
    async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: SharkIqConfigEntry
) -> bool:
    """Initialize the sharkiq platform via config entry."""
    if CONF_REGION not in config_entry.data:
        hass.config_entries.async_update_entry(
            config_entry,
            data={**config_entry.data, CONF_REGION: SHARKIQ_REGION_DEFAULT},
        )

    europe = config_entry.data.get(CONF_REGION) == SHARKIQ_REGION_EUROPE

    websession = async_create_clientsession(
        hass,
        cookie_jar=aiohttp.CookieJar(unsafe=True, quote_cookie=False),
        family=socket.AF_INET,
    )

    coordinator: SharkIqUpdateCoordinator | None = None
    stored_backend = config_entry.data.get(CONF_BACKEND)

    # Try backends in order of preference
    if stored_backend != BACKEND_AYLA:
        # Try skegox first (newer backend, accounts are being migrated to it)
        coordinator = await _async_setup_skegox(
            hass, config_entry, websession, europe
        )
        if coordinator:
            _update_backend(hass, config_entry, BACKEND_SKEGOX)

    if coordinator is None and stored_backend != BACKEND_SKEGOX:
        # Fall back to Ayla (legacy backend)
        coordinator = await _async_setup_ayla(
            hass, config_entry, websession, europe
        )
        if coordinator:
            _update_backend(hass, config_entry, BACKEND_AYLA)

    if coordinator is None:
        # Try whichever we haven't tried yet
        if stored_backend == BACKEND_AYLA:
            coordinator = await _async_setup_skegox(
                hass, config_entry, websession, europe
            )
            if coordinator:
                _update_backend(hass, config_entry, BACKEND_SKEGOX)
        elif stored_backend == BACKEND_SKEGOX:
            coordinator = await _async_setup_ayla(
                hass, config_entry, websession, europe
            )
            if coordinator:
                _update_backend(hass, config_entry, BACKEND_AYLA)

    if coordinator is None:
        raise exceptions.ConfigEntryNotReady(
            "Could not connect to SharkIQ via either Ayla or Skegox backend"
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

    The integration has gone through three room-cleaning UX iterations:

    1. One ``button.<vac>_clean_<room>`` per room — replaced in iteration 2.
    2. Queue switches + clean-mode select + single start button — replaced
       in iteration 3 because the composer was clunky to use on a Lovelace
       card.
    3. Per-preset buttons configured via the options flow (current).

    Without explicit cleanup, registry rows from prior iterations linger
    forever and clutter every device card. We match by unique_id prefix
    so we don't accidentally remove the new ``{serial}_preset_...`` rows.
    """
    registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(registry, config_entry.entry_id)
    serials = set(coordinator.shark_vacs)
    # (entity_domain, unique_id_substring) — substring lookup keeps us safe
    # when prefix shapes shift (e.g. "{serial}_clean_<room>" vs
    # "{serial}_start_room_clean").
    stale_substrings_by_domain: dict[str, tuple[str, ...]] = {
        "button": ("_clean_", "_start_room_clean"),
        "select": ("_clean_type",),
        "switch": ("_queue_",),
    }
    for entry in entries:
        substrings = stale_substrings_by_domain.get(entry.domain)
        if substrings is None:
            continue
        unique_id = entry.unique_id or ""
        # Skip new-style preset buttons even though they share a domain.
        if "_preset_" in unique_id:
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


def _update_backend(
    hass: HomeAssistant, config_entry: SharkIqConfigEntry, backend: str
) -> None:
    """Store the working backend in config entry data."""
    if config_entry.data.get(CONF_BACKEND) != backend:
        hass.config_entries.async_update_entry(
            config_entry,
            data={**config_entry.data, CONF_BACKEND: backend},
        )


async def async_disconnect_or_timeout(coordinator: SharkIqUpdateCoordinator) -> None:
    """Disconnect from API."""
    LOGGER.debug("Disconnecting from Shark API")
    async with asyncio.timeout(5):
        if coordinator.ayla_api is not None:
            with suppress(
                SharkIqAuthError, SharkIqAuthExpiringError, SharkIqNotAuthedError
            ):
                await coordinator.ayla_api.async_sign_out()


async def async_unload_entry(
    hass: HomeAssistant, config_entry: SharkIqConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )
    if unload_ok:
        with suppress(SharkIqAuthError):
            await async_disconnect_or_timeout(coordinator=config_entry.runtime_data)
        # Drop this entry's rooms from the service dropdown.
        async_refresh_clean_room_schema(hass)

    return unload_ok
