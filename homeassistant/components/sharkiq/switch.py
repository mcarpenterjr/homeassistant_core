"""Per-room selection switches for Shark IQ vacuums.

Home Assistant's stock dashboard cards have no multi-select widget, so the
"pick rooms, then start cleaning" UX can't live inside a single tile. The
workaround that survives without a HACS custom card is to surface each
room as its own switch and add a companion button that reads the on/off
state across the set. The auto-generated device card then groups every
switch alongside the vacuum entity, and the user gets a checklist-style
picker out of the box.

State persists across HA restarts via :class:`RestoreEntity` so the user's
selection survives a reboot. The companion ``Clean selected rooms`` button
resets all switches off after dispatching, which is the most common
expected behavior — pick rooms, clean, switches clear.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import DOMAIN, SHARK
from .coordinator import SharkDevice, SharkIqConfigEntry, SharkIqUpdateCoordinator


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create one selection switch per (vacuum, room) pair.

    Only emits switches for vacuums that have MARD-derived display rooms —
    legacy Ayla devices and skegox devices whose MARD didn't parse just
    don't get the picker. Falling back to ``Robot_Room_List`` would yield
    raw ``AZ_N`` names that don't match what the user sees in the app.
    """
    coordinator = config_entry.runtime_data
    entities: list[SharkRoomSelectSwitch] = []
    for device in coordinator.shark_vacs.values():
        display_rooms = getattr(device, "display_rooms", None) or {}
        for room_name in display_rooms:
            entities.append(SharkRoomSelectSwitch(device, coordinator, room_name))
    if entities:
        async_add_entities(entities)


class SharkRoomSelectSwitch(
    CoordinatorEntity[SharkIqUpdateCoordinator],
    SwitchEntity,
    RestoreEntity,
):
    """Stateful switch marking one room as 'queued for the next clean'.

    The state is purely UI selection — toggling has no device-side effect
    on its own. The companion ``Clean selected rooms`` button reads these
    switches and dispatches the clean.
    """

    _attr_has_entity_name = True
    _attr_icon = "mdi:checkbox-marked-outline"

    def __init__(
        self,
        sharkiq: SharkDevice,
        coordinator: SharkIqUpdateCoordinator,
        room_name: str,
    ) -> None:
        """Bind to a parent SharkDevice and one display room."""
        super().__init__(coordinator)
        self._sharkiq = sharkiq
        self._room_name = room_name
        slug = slugify(room_name)
        self._attr_unique_id = f"{sharkiq.serial_number}_select_{slug}"
        # Entity name is the bare room — the device name in DeviceInfo
        # already prefixes it on the device card (eg "Clean Latifah Kitchen").
        # Selection semantics come from the switch being toggleable, not
        # from the label.
        self._attr_name = room_name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sharkiq.serial_number)},
            manufacturer=SHARK,
            name=sharkiq.name,
        )
        self._attr_is_on = False

    @property
    def room_name(self) -> str:
        """Return the display name of the room this switch is bound to.

        Consumed by the companion ``Clean selected rooms`` button so it
        can pass the room name verbatim to the ``clean_room`` service
        without round-tripping through slug derivation.
        """
        return self._room_name

    async def async_added_to_hass(self) -> None:
        """Restore the last on/off state across HA restarts."""
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._attr_is_on = last_state.state == STATE_ON

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Mark this room as selected."""
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Unselect this room."""
        self._attr_is_on = False
        self.async_write_ha_state()
