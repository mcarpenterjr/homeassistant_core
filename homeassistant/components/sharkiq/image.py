"""Floor-plan image entity for Shark IQ vacuums.

One entity per skegox vacuum that has a parseable MARD: renders the
device's room polygons as an SVG so any standard Lovelace ``picture`` or
``image`` card can drop the floor plan onto a dashboard. Older Ayla devices
and devices without MARD data don't get the entity.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.components.image import ImageEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, LOGGER, SHARK
from .coordinator import SharkDevice, SharkIqConfigEntry, SharkIqUpdateCoordinator
from .floor_plan import Area, render_floor_plan_svg


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create a floor-plan image entity for each device that has MARD geometry."""
    coordinator = config_entry.runtime_data
    entities: list[SharkIqFloorPlanImage] = []
    for device in coordinator.shark_vacs.values():
        # Gate on ``mard_areas`` existence rather than the backend type so we
        # automatically fan in any future device shadow that exposes MARD-
        # shaped geometry. Devices without it (legacy Ayla, fresh devices
        # that haven't been mapped yet) simply don't get this entity rather
        # than surfacing a permanently-empty one.
        if not getattr(device, "mard_areas", None):
            LOGGER.debug(
                "Skipping floor plan for %s: no MARD polygons available",
                device.serial_number,
            )
            continue
        entities.append(SharkIqFloorPlanImage(hass, device, coordinator))
    if entities:
        async_add_entities(entities)


class SharkIqFloorPlanImage(
    CoordinatorEntity[SharkIqUpdateCoordinator], ImageEntity
):
    """SVG floor plan rendered from MARD polygons."""

    _attr_has_entity_name = True
    _attr_translation_key = "floor_plan"
    _attr_content_type = "image/svg+xml"

    def __init__(
        self,
        hass: HomeAssistant,
        device: SharkDevice,
        coordinator: SharkIqUpdateCoordinator,
    ) -> None:
        """Bind to a parent SharkDevice and seed the image timestamp."""
        CoordinatorEntity.__init__(self, coordinator)
        ImageEntity.__init__(self, hass)
        self._device = device
        self._attr_unique_id = f"{device.serial_number}_floor_plan"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.serial_number)},
            manufacturer=SHARK,
            name=device.name,
        )
        # Seed the timestamp so the frontend has something to key its cache
        # on. We bump it only when the underlying MARD areas actually change
        # — the coordinator polls device state every 30 s while cleaning but
        # the floor plan is essentially static between mapping runs.
        self._attr_image_last_updated = dt_util.utcnow()
        self._areas_signature: tuple[tuple[str, str], ...] = self._signature()

    def _signature(self) -> tuple[tuple[str, str], ...]:
        """Compact fingerprint of the current MARD areas for change detection.

        ``(display_name, uuid)`` per area is enough to detect renames,
        merges, and adds/removes without serializing the whole geometry on
        every poll.
        """
        areas = getattr(self._device, "mard_areas", None) or []
        return tuple(
            (a.get("display_name", ""), a.get("uuid", "")) for a in areas
        )

    def _handle_coordinator_update(self) -> None:
        """Refresh the cache timestamp only when the MARD areas changed."""
        new_sig = self._signature()
        if new_sig != self._areas_signature:
            self._areas_signature = new_sig
            self._attr_image_last_updated = dt_util.utcnow()
            self._cached_image = None
        super()._handle_coordinator_update()

    def image(self) -> bytes | None:
        """Render the SVG synchronously — generation is cheap, no I/O."""
        raw_areas = getattr(self._device, "mard_areas", None)
        if not raw_areas:
            return None
        areas: list[Area] = [
            {
                "display_name": a.get("display_name", ""),
                "points": list(a.get("points") or []),
                "uuid": a.get("uuid", ""),
            }
            for a in raw_areas
        ]
        return render_floor_plan_svg(areas, title=f"{self._device.name} floor plan")
