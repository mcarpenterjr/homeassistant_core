"""Buttons for Shark IQ vacuums.

Two flavors:

- :class:`SharkPresetCleanButton` — one per user-defined cleaning preset.
  Bundles rooms + clean mode + fan speed; one tap dispatches ``clean_room``.
- :class:`SharkCleanSelectedRoomsButton` — one per vacuum. Reads the
  vacuum's per-room "Select" switches and dispatches a clean of every
  selected room, then resets the switches off. This is the ad-hoc picker
  HA's dashboard primitives can't provide directly (no multi-select widget
  outside of service-call forms).
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.vacuum import DOMAIN as VACUUM_DOMAIN
from homeassistant.const import STATE_ON
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
    """Create preset buttons + one Clean-selected button per vacuum."""
    coordinator = config_entry.runtime_data
    presets = config_entry.options.get(CONF_PRESETS, [])
    entities: list[ButtonEntity] = []
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

    # Only emit a "Clean selected rooms" button for vacuums that actually
    # have per-room select switches — i.e. MARD-derived display rooms.
    # Without switches the button would always be a no-op.
    for device in coordinator.shark_vacs.values():
        if getattr(device, "display_rooms", None):
            entities.append(SharkCleanSelectedRoomsButton(device, coordinator))

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


class SharkCleanSelectedRoomsButton(
    CoordinatorEntity[SharkIqUpdateCoordinator], ButtonEntity
):
    """Dispatches a clean across every room whose select switch is on.

    The room set is read live from the entity registry + state machine,
    not cached, so the user can toggle switches in any order before
    pressing this button without the integration having to track the
    selection itself. After dispatch the matching switches are turned off
    so the next clean starts from a fresh selection — presets exist for
    the "save this combo" use case.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:broom"
    _attr_translation_key = "clean_selected_rooms"

    def __init__(
        self,
        sharkiq: SharkDevice,
        coordinator: SharkIqUpdateCoordinator,
    ) -> None:
        """Bind to a parent SharkDevice."""
        super().__init__(coordinator)
        self._sharkiq = sharkiq
        self._attr_unique_id = f"{sharkiq.serial_number}_clean_selected"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sharkiq.serial_number)},
            manufacturer=SHARK,
            name=sharkiq.name,
        )

    @property
    def available(self) -> bool:
        """Available when the vacuum is online and the coordinator is fresh."""
        return (
            self.coordinator.has_recent_success
            and self.coordinator.device_is_online(self._sharkiq.serial_number)
        )

    async def async_press(self) -> None:
        """Read selected rooms from this vacuum's switches and dispatch."""
        registry = er.async_get(self.hass)
        serial = self._sharkiq.serial_number

        # Look the switches up via the registry rather than constructing the
        # entity IDs from slugs — users can rename entity IDs, but the
        # unique_id is stable. The runtime ``data`` map carries the entity
        # objects, which we need for both reading state and turning them off.
        select_switches: dict[str, str] = {}
        for entity in er.async_entries_for_config_entry(
            registry, self.coordinator.config_entry.entry_id
        ):
            if entity.domain != SWITCH_DOMAIN:
                continue
            unique_id = entity.unique_id or ""
            prefix = f"{serial}_select_"
            if not unique_id.startswith(prefix):
                continue
            select_switches[entity.entity_id] = unique_id

        rooms: list[str] = []
        on_entity_ids: list[str] = []
        for entity_id in select_switches:
            state = self.hass.states.get(entity_id)
            if state is None or state.state != STATE_ON:
                continue
            on_entity_ids.append(entity_id)
            # The unique_id slug is lossy for room names with punctuation,
            # so resolve the display name from the switch's friendly_name
            # (set to "Select <Room>" at construction time).
            rooms.append(_resolve_room_name(entity_id, state.attributes))

        if not rooms:
            raise HomeAssistantError(
                "No rooms are selected — toggle at least one Select switch "
                "on before pressing Clean selected rooms"
            )

        vacuum_entity_id = registry.async_get_entity_id(
            VACUUM_DOMAIN, DOMAIN, serial
        )
        if vacuum_entity_id is None:
            raise HomeAssistantError(
                f"Vacuum entity for {self._sharkiq.name} not found"
            )

        LOGGER.debug(
            "Clean Selected pressed for %s: %s", self._sharkiq.name, rooms
        )
        await self.hass.services.async_call(
            DOMAIN,
            SERVICE_CLEAN_ROOM,
            {ATTR_ROOMS: rooms},
            target={"entity_id": vacuum_entity_id},
            blocking=True,
        )

        # Reset the switches that participated in this clean. Use the
        # generic switch.turn_off so the SharkRoomSelectSwitch's own
        # async_turn_off persists state through RestoreEntity.
        for entity_id in on_entity_ids:
            await self.hass.services.async_call(
                SWITCH_DOMAIN,
                "turn_off",
                {"entity_id": entity_id},
                blocking=False,
            )


def _resolve_room_name(entity_id: str, attributes: dict[str, Any]) -> str:
    """Return the display room name a select switch represents.

    Tries the entity's friendly name first (the switch sets it to
    ``Select <Room>``), falls back to the entity_id slug for safety.
    """
    friendly = attributes.get("friendly_name")
    if isinstance(friendly, str) and friendly.startswith("Select "):
        return friendly[len("Select ") :]
    # Last-resort fallback: turn "switch.<vac>_select_<room_slug>" into
    # something we can pass — title-case the slug.
    object_id = entity_id.split(".", 1)[-1]
    _, _, slug = object_id.partition("_select_")
    return slug.replace("_", " ").title()
