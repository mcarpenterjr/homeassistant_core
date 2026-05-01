"""Composer 'Start room clean' button for Shark IQ vacuums."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, LOGGER, SHARK
from .coordinator import SharkDevice, SharkIqConfigEntry, SharkIqUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one start-clean button per Shark IQ vacuum."""
    coordinator = config_entry.runtime_data
    devices: Iterable[SharkDevice] = coordinator.shark_vacs.values()
    async_add_entities(SharkStartRoomCleanButton(d, coordinator) for d in devices)


class SharkStartRoomCleanButton(
    CoordinatorEntity[SharkIqUpdateCoordinator], ButtonEntity
):
    """Press to start a clean of all rooms currently toggled in the queue."""

    _attr_has_entity_name = True
    _attr_translation_key = "start_room_clean"
    _attr_icon = "mdi:play-circle"

    def __init__(
        self, sharkiq: SharkDevice, coordinator: SharkIqUpdateCoordinator
    ) -> None:
        """Initialize the start button."""
        super().__init__(coordinator)
        self._sharkiq = sharkiq
        self._attr_unique_id = f"{sharkiq.serial_number}_start_room_clean"
        self._attr_name = "Start room clean"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sharkiq.serial_number)},
            manufacturer=SHARK,
            name=sharkiq.name,
        )

    @property
    def available(self) -> bool:
        """Available when the vacuum is online and we've had a recent success."""
        return (
            self.coordinator.has_recent_success
            and self.coordinator.device_is_online(self._sharkiq.serial_number)
        )

    async def async_press(self) -> None:
        """Trigger a clean of every room currently queued."""
        dsn = self._sharkiq.serial_number
        queued = sorted(self.coordinator.room_queue.get(dsn, set()))
        if not queued:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="empty_room_queue",
            )
        clean_type = self.coordinator.clean_type.get(dsn, "dry")
        LOGGER.debug(
            "Starting room clean for %s: rooms=%s clean_type=%s",
            self._sharkiq.name,
            queued,
            clean_type,
        )
        if self.coordinator.is_skegox:
            await self._sharkiq.async_clean_rooms(queued, clean_type=clean_type)
        else:
            await self._sharkiq.async_clean_rooms(queued)
        await self.coordinator.async_refresh()
