"""Per-room queue switches for Shark IQ vacuums."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER, SHARK
from .coordinator import SharkDevice, SharkIqConfigEntry, SharkIqUpdateCoordinator


def _device_rooms(sharkiq: SharkDevice) -> list[str]:
    """Return the list of named rooms reported by the vacuum."""
    try:
        room_list = sharkiq.get_property_value("Robot_Room_List")
    except KeyError:
        return []
    if not room_list or ":" not in room_list:
        return []
    return room_list.split(":")[1:]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one queue switch per (device, room)."""
    coordinator = config_entry.runtime_data
    devices: Iterable[SharkDevice] = coordinator.shark_vacs.values()

    entities: list[SharkRoomQueueSwitch] = []
    seen: set[tuple[str, str]] = set()
    for device in devices:
        rooms = _device_rooms(device)
        if not rooms:
            LOGGER.debug(
                "%s reported no named rooms — no queue switches created",
                device.name,
            )
            continue
        for room in rooms:
            key = (device.serial_number, room)
            if key in seen:
                continue
            seen.add(key)
            entities.append(SharkRoomQueueSwitch(device, coordinator, room))

    if entities:
        async_add_entities(entities)


class SharkRoomQueueSwitch(
    CoordinatorEntity[SharkIqUpdateCoordinator], SwitchEntity, RestoreEntity
):
    """Toggle to mark a room for the next clean run.

    These entities don't talk to the vacuum themselves — they just maintain
    a per-device queue on the coordinator. The "Start room clean" button
    reads the queue and triggers a single multi-room clean using the mode
    selected on the clean-type select entity.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "queue_room"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:floor-plan"

    def __init__(
        self,
        sharkiq: SharkDevice,
        coordinator: SharkIqUpdateCoordinator,
        room: str,
    ) -> None:
        """Initialize a queue switch."""
        super().__init__(coordinator)
        self._sharkiq = sharkiq
        self._room = room
        self._attr_unique_id = f"{sharkiq.serial_number}_queue_{room}"
        self._attr_translation_placeholders = {"room": room}
        # Fallback name for UIs without translations.
        self._attr_name = f"Queue {room}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sharkiq.serial_number)},
            manufacturer=SHARK,
            name=sharkiq.name,
        )
        self._attr_is_on = False

    @property
    def available(self) -> bool:
        """Available whenever the room is still in the device's room list."""
        return self._room in _device_rooms(self._sharkiq)

    async def async_added_to_hass(self) -> None:
        """Restore last on/off state and rebuild the coordinator queue."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None and last.state == "on":
            self._attr_is_on = True
            self.coordinator.room_queue.setdefault(
                self._sharkiq.serial_number, set()
            ).add(self._room)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Add the room to the coordinator's queue."""
        self._attr_is_on = True
        self.coordinator.room_queue.setdefault(
            self._sharkiq.serial_number, set()
        ).add(self._room)
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Remove the room from the coordinator's queue."""
        self._attr_is_on = False
        self.coordinator.room_queue.setdefault(
            self._sharkiq.serial_number, set()
        ).discard(self._room)
        self.async_write_ha_state()
