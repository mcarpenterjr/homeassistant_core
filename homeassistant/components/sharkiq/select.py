"""Clean-mode selector for Shark IQ vacuums."""

from __future__ import annotations

from collections.abc import Iterable

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CLEAN_TYPES, DOMAIN, SHARK
from .coordinator import SharkDevice, SharkIqConfigEntry, SharkIqUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up clean-mode select entities for each Shark IQ vacuum."""
    coordinator = config_entry.runtime_data
    devices: Iterable[SharkDevice] = coordinator.shark_vacs.values()
    async_add_entities(SharkCleanTypeSelect(d, coordinator) for d in devices)


class SharkCleanTypeSelect(
    CoordinatorEntity[SharkIqUpdateCoordinator], SelectEntity, RestoreEntity
):
    """Lets the user pick the clean mode used for queued room cleans."""

    _attr_has_entity_name = True
    _attr_translation_key = "clean_type"
    _attr_options = list(CLEAN_TYPES)
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:robot-vacuum-variant"

    def __init__(
        self, sharkiq: SharkDevice, coordinator: SharkIqUpdateCoordinator
    ) -> None:
        """Initialize the clean-type select."""
        super().__init__(coordinator)
        self._sharkiq = sharkiq
        self._attr_unique_id = f"{sharkiq.serial_number}_clean_type"
        self._attr_current_option = "dry"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sharkiq.serial_number)},
            manufacturer=SHARK,
            name=sharkiq.name,
        )

    async def async_added_to_hass(self) -> None:
        """Restore the previously chosen clean type after HA restarts."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last and last.state in CLEAN_TYPES:
            self._attr_current_option = last.state
        # Mirror the restored value onto the coordinator so the start button
        # picks up the right mode without waiting for the user to re-select.
        self.coordinator.clean_type[self._sharkiq.serial_number] = (
            self._attr_current_option or "dry"
        )

    async def async_select_option(self, option: str) -> None:
        """Persist the chosen clean type and propagate to the coordinator."""
        self._attr_current_option = option
        self.coordinator.clean_type[self._sharkiq.serial_number] = option
        self.async_write_ha_state()
