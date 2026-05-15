"""Per-preset 'Clean rooms' buttons for Shark IQ vacuums.

Each cleaning preset configured in the integration's options flow becomes
a button on the vacuum's device card. Pressing the button dispatches the
``sharkiq.clean_room`` service against the target vacuum with the rooms,
clean mode, and fan speed bundled in the preset, so all the room-cleaning
logic lives in one place (the service / vacuum entity method) and the
button is just a one-tap shortcut.

Ad-hoc multi-room cleans use the vacuum tile's Start button: when any of
the vacuum's ``Select …`` switches are on, Start dispatches ``clean_room``
against those rooms instead of doing a full clean. The room picker is the
switches; the dispatcher is the standard Start button.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_CLEAN_TYPE,
    ATTR_FAN_SPEED,
    ATTR_ROOMS,
    CONF_PRESETS,
    DOMAIN,
    LOGGER,
    PRESET_CLEAN_TYPE,
    PRESET_FAN_SPEED,
    PRESET_ID,
    PRESET_NAME,
    PRESET_ROOMS,
    PRESET_SERIAL,
    SHARK,
)
from .coordinator import SharkDevice, SharkIqConfigEntry, SharkIqUpdateCoordinator
from .services import SERVICE_CLEAN_ROOM


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one button per configured cleaning preset."""
    coordinator = config_entry.runtime_data
    presets = config_entry.options.get(CONF_PRESETS, [])
    entities: list[SharkPresetCleanButton] = []
    for preset in presets:
        serial = preset.get(PRESET_SERIAL)
        device = coordinator.shark_vacs.get(serial)
        if device is None:
            LOGGER.debug(
                "Skipping preset %s: target vacuum %s not in this entry",
                preset.get(PRESET_NAME),
                serial,
            )
            continue
        entities.append(SharkPresetCleanButton(device, coordinator, preset))
    if entities:
        async_add_entities(entities)


class SharkPresetCleanButton(
    CoordinatorEntity[SharkIqUpdateCoordinator], ButtonEntity
):
    """One-tap clean of the rooms bundled in a user-defined preset."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:robot-vacuum"

    def __init__(
        self,
        sharkiq: SharkDevice,
        coordinator: SharkIqUpdateCoordinator,
        preset: dict[str, Any],
    ) -> None:
        """Initialize a preset clean button."""
        super().__init__(coordinator)
        self._sharkiq = sharkiq
        self._preset = preset
        preset_id = preset.get(PRESET_ID) or preset.get(PRESET_NAME, "preset")
        self._attr_unique_id = f"{sharkiq.serial_number}_preset_{preset_id}"
        self._attr_name = f"Clean {preset.get(PRESET_NAME, 'preset')}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sharkiq.serial_number)},
            manufacturer=SHARK,
            name=sharkiq.name,
        )

    @property
    def available(self) -> bool:
        """Available when the vacuum is online and we've had a recent success.

        Uses the coordinator's grace-windowed success flag so a single
        failed poll on a flaky network doesn't yank the button out from
        under the user — matches ``vacuum.SharkVacuumEntity.available``.
        """
        return (
            self.coordinator.has_recent_success
            and self.coordinator.device_is_online(self._sharkiq.serial_number)
        )

    async def async_press(self) -> None:
        """Trigger a clean of the preset's rooms via the clean_room service."""
        rooms = list(self._preset.get(PRESET_ROOMS) or [])
        if not rooms:
            raise HomeAssistantError(
                f"Preset '{self._preset.get(PRESET_NAME)}' has no rooms configured"
            )

        registry = er.async_get(self.hass)
        vacuum_entity_id = registry.async_get_entity_id(
            VACUUM_DOMAIN, DOMAIN, self._sharkiq.serial_number
        )
        if vacuum_entity_id is None:
            raise HomeAssistantError(
                f"Vacuum entity for {self._sharkiq.name} not found"
            )

        service_data: dict[str, Any] = {ATTR_ROOMS: rooms}
        if (clean_type := self._preset.get(PRESET_CLEAN_TYPE)):
            service_data[ATTR_CLEAN_TYPE] = clean_type
        if (fan_speed := self._preset.get(PRESET_FAN_SPEED)):
            service_data[ATTR_FAN_SPEED] = fan_speed

        LOGGER.debug(
            "Pressing preset button %s for %s: %s",
            self._preset.get(PRESET_NAME),
            self._sharkiq.name,
            service_data,
        )
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_CLEAN_ROOM,
            service_data,
            target={"entity_id": vacuum_entity_id},
            blocking=True,
        )
