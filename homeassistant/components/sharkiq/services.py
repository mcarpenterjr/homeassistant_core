"""Shark IQ services."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, service

from .const import (
    ATTR_CLEAN_TYPE,
    ATTR_FAN_SPEED,
    ATTR_ROOMS,
    CLEAN_TYPES,
    DOMAIN,
    FAN_SPEED_NAMES,
    LOGGER,
)

SERVICE_CLEAN_ROOM = "clean_room"


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register integration-level services.

    The clean_room service is registered with a static fallback schema here.
    Per-device room dropdowns are layered on top via async_refresh_services
    once each config entry has finished loading and we know that device's
    actual room list.
    """

    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_CLEAN_ROOM,
        entity_domain=VACUUM_DOMAIN,
        schema={
            vol.Required(ATTR_ROOMS): vol.All(
                cv.ensure_list, vol.Length(min=1), [cv.string]
            ),
            vol.Optional(ATTR_CLEAN_TYPE, default="dry"): vol.In(CLEAN_TYPES),
            vol.Optional(ATTR_FAN_SPEED): vol.In(FAN_SPEED_NAMES),
        },
        func="async_clean_room",
    )


@callback
def async_refresh_clean_room_schema(hass: HomeAssistant) -> None:
    """Rewrite the clean_room service description with current device rooms.

    HA's automation editor reads service field selectors from the description
    cache rather than the Voluptuous schema. Updating it after each config
    entry sets up gives users a real per-device room dropdown instead of a
    text input. Rooms are unioned across all loaded entries so multi-vacuum
    households still work; ``custom_value`` keeps the service usable if the
    coordinator hasn't refreshed yet (the dropdown is a hint, not a
    constraint — the entity itself still validates the room list).
    """
    rooms: set[str] = set()
    # Late import to avoid a circular dependency at module load.
    from . import _all_loaded_coordinators

    for coordinator in _all_loaded_coordinators(hass):
        for vac in coordinator.shark_vacs.values():
            try:
                room_list = vac.get_property_value("Robot_Room_List")
            except KeyError:
                continue
            if not room_list or ":" not in room_list:
                continue
            rooms.update(room_list.split(":")[1:])

    description: dict[str, Any] = {
        "name": "Clean room",
        "description": (
            "Cleans a specific user-defined room or set of rooms. "
            "Optionally pick a clean mode and fan speed for the run."
        ),
        "target": {"entity": {"integration": DOMAIN, "domain": VACUUM_DOMAIN}},
        "fields": {
            ATTR_ROOMS: {
                "name": "Rooms",
                "description": (
                    "Rooms to clean. Names must match the SharkClean app exactly."
                ),
                "required": True,
                "selector": {
                    "select": {
                        "options": sorted(rooms) or [],
                        "multiple": True,
                        "custom_value": True,
                    }
                },
            },
            ATTR_CLEAN_TYPE: {
                "name": "Clean type",
                "description": (
                    "Cleaning mode (newer hybrid devices only). "
                    "'dry' vacuums; 'wet' uses the mop pad; 'matrix' is a "
                    "denser pattern; 'spot' focuses on a small area."
                ),
                "default": "dry",
                "selector": {
                    "select": {"options": list(CLEAN_TYPES)}
                },
            },
            ATTR_FAN_SPEED: {
                "name": "Fan speed",
                "description": (
                    "Suction power applied to the device before cleaning starts."
                ),
                "selector": {
                    "select": {"options": list(FAN_SPEED_NAMES)}
                },
            },
        },
    }

    LOGGER.debug(
        "Refreshing sharkiq.clean_room service schema with %d room(s)", len(rooms)
    )
    service.async_set_service_schema(hass, DOMAIN, SERVICE_CLEAN_ROOM, description)
