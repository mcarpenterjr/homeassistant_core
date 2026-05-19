"""Shark IQ Wrapper."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sharkiq import OperatingModes, PowerModes, Properties

from homeassistant.components.image import DOMAIN as IMAGE_DOMAIN
from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    ATTR_ROOMS,
    CONF_PRESETS,
    DOMAIN,
    LOGGER,
    PRESET_ID,
    PRESET_NAME,
    PRESET_SERIAL,
    SHARK,
)
from .coordinator import SharkDevice, SharkIqConfigEntry, SharkIqUpdateCoordinator
from .dashboard_yaml import build_vacuum_dashboard_yaml

OPERATING_STATE_MAP = {
    OperatingModes.PAUSE: VacuumActivity.PAUSED,
    OperatingModes.START: VacuumActivity.CLEANING,
    OperatingModes.STOP: VacuumActivity.IDLE,
    OperatingModes.RETURN: VacuumActivity.RETURNING,
}

# Integer-based state map for skegox devices (same values, no enum wrapper)
OPERATING_STATE_INT_MAP = {
    0: VacuumActivity.IDLE,      # STOP
    1: VacuumActivity.PAUSED,    # PAUSE
    2: VacuumActivity.CLEANING,  # START
    3: VacuumActivity.RETURNING, # RETURN
}

FAN_SPEEDS_MAP = {
    "Eco": PowerModes.ECO,
    "Normal": PowerModes.NORMAL,
    "Max": PowerModes.MAX,
}

STATE_RECHARGING_TO_RESUME = "recharging_to_resume"

# Attributes to expose
ATTR_ERROR_CODE = "last_error_code"
ATTR_ERROR_MSG = "last_error_message"
ATTR_LOW_LIGHT = "low_light"
ATTR_RECHARGE_RESUME = "recharge_and_resume"
ATTR_SCHEDULE = "schedule"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: SharkIqConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the Shark IQ vacuum cleaner."""
    coordinator = config_entry.runtime_data
    devices: Iterable[SharkDevice] = coordinator.shark_vacs.values()
    device_names = [d.name for d in devices]
    LOGGER.debug(
        "Found %d Shark IQ device(s): %s",
        len(device_names),
        ", ".join(device_names),
    )
    async_add_entities([SharkVacuumEntity(d, coordinator) for d in devices])


class SharkVacuumEntity(CoordinatorEntity[SharkIqUpdateCoordinator], StateVacuumEntity):
    """Shark IQ vacuum entity."""

    _attr_fan_speed_list = list(FAN_SPEEDS_MAP)
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = (
        VacuumEntityFeature.BATTERY
        | VacuumEntityFeature.FAN_SPEED
        | VacuumEntityFeature.PAUSE
        | VacuumEntityFeature.RETURN_HOME
        | VacuumEntityFeature.START
        | VacuumEntityFeature.STATE
        | VacuumEntityFeature.STOP
        | VacuumEntityFeature.LOCATE
    )
    _unrecorded_attributes = frozenset({ATTR_ROOMS, ATTR_SCHEDULE})

    def __init__(
        self, sharkiq: SharkDevice, coordinator: SharkIqUpdateCoordinator
    ) -> None:
        """Create a new SharkVacuumEntity."""
        super().__init__(coordinator)
        self.sharkiq = sharkiq
        self._attr_unique_id = sharkiq.serial_number
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, sharkiq.serial_number)},
            manufacturer=SHARK,
            model=self.model,
            name=sharkiq.name,
            sw_version=sharkiq.get_property_value(Properties.ROBOT_FIRMWARE_VERSION),
        )

    def clean_spot(self, **kwargs: Any) -> None:
        """Clean a spot. Not yet implemented."""
        raise NotImplementedError

    def send_command(
        self,
        command: str,
        params: dict[str, Any] | list[Any] | None = None,
        **kwargs: Any,
    ) -> None:
        """Send a command to the vacuum. Not yet implemented."""
        raise NotImplementedError

    @property
    def is_online(self) -> bool:
        """Tell us if the device is online."""
        return self.coordinator.device_is_online(self.sharkiq.serial_number)

    @property
    def model(self) -> str:
        """Vacuum model number."""
        if self.sharkiq.vac_model_number:
            return self.sharkiq.vac_model_number
        return self.sharkiq.oem_model_number

    @property
    def error_code(self) -> int | None:
        """Return the last observed error code (or None)."""
        return self.sharkiq.error_code

    @property
    def error_message(self) -> str | None:
        """Return the last observed error message (or None)."""
        if not self.error_code:
            return None
        return self.sharkiq.error_text

    @property
    def recharging_to_resume(self) -> int | None:
        """Return True if vacuum set to recharge and resume cleaning."""
        return self.sharkiq.get_property_value(Properties.RECHARGING_TO_RESUME)

    @property
    def activity(self) -> VacuumActivity | None:
        """Get the current vacuum state.

        NB: Currently, we do not return an error state because they can be very, very stale.
        In the app, these are (usually) handled by showing the robot as stopped and sending the
        user a notification.
        """
        if self.sharkiq.get_property_value(Properties.CHARGING_STATUS):
            return VacuumActivity.DOCKED
        op_mode = self.sharkiq.get_property_value(Properties.OPERATING_MODE)
        # Try enum-based map first (Ayla), fall back to int-based (Skegox)
        result = OPERATING_STATE_MAP.get(op_mode)
        if result is None and isinstance(op_mode, int):
            result = OPERATING_STATE_INT_MAP.get(op_mode)
        return result

    @property
    def available(self) -> bool:
        """Available if the device is online and recent polls have been OK.

        Uses the coordinator's grace-windowed success flag rather than the
        raw ``last_update_success``, so a single failed poll on a flaky
        network doesn't yank the entity out from under the user.
        """
        return self.is_online and self.coordinator.has_recent_success

    @property
    def battery_level(self) -> int | None:
        """Get the current battery level."""
        return self.sharkiq.get_property_value(Properties.BATTERY_CAPACITY)

    async def async_return_to_base(self, **kwargs: Any) -> None:
        """Have the device return to base."""
        await self.sharkiq.async_set_operating_mode(OperatingModes.RETURN)
        await self.coordinator.async_refresh()

    async def async_pause(self) -> None:
        """Pause the cleaning task."""
        await self.sharkiq.async_set_operating_mode(OperatingModes.PAUSE)
        await self.coordinator.async_refresh()

    async def async_start(self) -> None:
        """Start the device.

        Selection-aware: if any of this vacuum's per-room ``Select …``
        switches are currently on, dispatches a ``clean_room`` against
        those rooms and resets the switches afterward. Otherwise falls
        through to the full-clean path. This folds the previous
        ``Clean selected rooms`` button into the vacuum tile's Start
        button so users have one entry point instead of two.

        HA's tile-card ``vacuum-commands`` feature draws the Start label
        verbatim — we can't rename it to "Start cleaning selected" based
        on state. The behavior is contextual; the label stays "Start".
        """
        selected = self._read_selected_rooms()
        LOGGER.debug(
            "Shark IQ Start pressed for %s: %d room switch(es) on -> %s",
            self.entity_id,
            len(selected),
            selected or "(no selection, full clean)",
        )
        if selected:
            await self.async_clean_room(rooms=selected)
            await self._reset_selected_switches()
            return

        await self.sharkiq.async_set_operating_mode(OperatingModes.START)
        await self.coordinator.async_refresh()

    def _read_selected_rooms(self) -> list[str]:
        """Return display room names whose select switch is currently on.

        Matches each switch unique_id (``{serial}_select_<slug>``) back to
        the device's canonical ``display_rooms`` mapping by slugifying the
        room name and comparing. This is robust against the switch's
        friendly_name being customised by the user — the unique_id is
        stable, and ``display_rooms`` is the authoritative source of room
        names on the device.
        """
        from homeassistant.util import slugify

        registry = er.async_get(self.hass)
        serial = self.sharkiq.serial_number
        entry_id = self.coordinator.config_entry.entry_id
        prefix = f"{serial}_select_"

        display_rooms = getattr(self.sharkiq, "display_rooms", None) or {}
        slug_to_room: dict[str, str] = {
            slugify(name): name for name in display_rooms
        }

        switches_on: list[str] = []
        orphans: list[str] = []
        rooms: list[str] = []
        for entry in er.async_entries_for_config_entry(registry, entry_id):
            if entry.domain != "switch":
                continue
            unique_id = entry.unique_id or ""
            if not unique_id.startswith(prefix):
                continue
            state = self.hass.states.get(entry.entity_id)
            if state is None or state.state != "on":
                continue
            switches_on.append(entry.entity_id)
            slug = unique_id[len(prefix) :]
            room = slug_to_room.get(slug)
            if room is None:
                orphans.append(f"{entry.entity_id} (slug={slug!r})")
                continue
            rooms.append(room)

        # DEBUG: retained as a diagnostic. The empty-rooms failure mode
        # (every switch shows up as an orphan because display_rooms is
        # empty) is the signal that MARD didn't load — kept here so a
        # future ``logger: debug`` flips it on without a code change.
        LOGGER.debug(
            "Shark IQ _read_selected_rooms: display_rooms=%d entries; "
            "switches on=%s; orphans=%s; resolved rooms=%s",
            len(display_rooms),
            switches_on or "[]",
            orphans or "[]",
            rooms or "[]",
        )
        return rooms

    async def _reset_selected_switches(self) -> None:
        """Turn off every ``Select …`` switch on this vacuum.

        Mirrors the previous ``Clean selected rooms`` button's reset
        behavior: dispatching the clean means the user "consumed" the
        selection, so the next press starts from a fresh state. Uses
        ``switch.turn_off`` via the service registry so each
        :class:`SharkRoomSelectSwitch` persists the new state through
        ``RestoreEntity``.
        """
        registry = er.async_get(self.hass)
        serial = self.sharkiq.serial_number
        entry_id = self.coordinator.config_entry.entry_id
        prefix = f"{serial}_select_"
        for entry in er.async_entries_for_config_entry(registry, entry_id):
            if entry.domain != "switch":
                continue
            if not (entry.unique_id or "").startswith(prefix):
                continue
            state = self.hass.states.get(entry.entity_id)
            if state is None or state.state != "on":
                continue
            await self.hass.services.async_call(
                "switch",
                "turn_off",
                {"entity_id": entry.entity_id},
                blocking=False,
            )

    async def async_stop(self, **kwargs: Any) -> None:
        """Stop the device."""
        await self.sharkiq.async_set_operating_mode(OperatingModes.STOP)
        await self.coordinator.async_refresh()

    async def async_locate(self, **kwargs: Any) -> None:
        """Cause the device to generate a loud chirp."""
        await self.sharkiq.async_find_device()

    async def async_clean_room(
        self,
        rooms: list[str],
        clean_type: str = "dry",
        fan_speed: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Clean specific rooms with an optional clean type and fan speed.

        ``clean_type`` selects between dry vacuum and wet mop on devices that
        support both modes. Only the skegox backend honors it; the legacy
        Ayla path silently ignores it.

        ``fan_speed``, when provided, is applied to the device before the
        clean starts so the whole batch runs at that suction power.
        """
        rooms_to_clean = []
        valid_rooms = self.available_rooms or []
        rooms = [room.replace("_", " ").title() for room in rooms]
        for room in rooms:
            if room in valid_rooms:
                rooms_to_clean.append(room)
            else:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_room",
                    translation_placeholders={"room": room},
                )

        if fan_speed is not None:
            normalized = fan_speed.capitalize()
            if normalized not in FAN_SPEEDS_MAP:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="invalid_fan_speed",
                    translation_placeholders={"fan_speed": fan_speed},
                )
            await self.sharkiq.async_set_property_value(
                Properties.POWER_MODE, FAN_SPEEDS_MAP[normalized]
            )

        LOGGER.debug(
            "Cleaning room(s) %s (clean_type=%s, fan_speed=%s)",
            rooms_to_clean,
            clean_type,
            fan_speed,
        )
        if self.coordinator.is_skegox:
            await self.sharkiq.async_clean_rooms(rooms_to_clean, clean_type=clean_type)
        else:
            await self.sharkiq.async_clean_rooms(rooms_to_clean)
        await self.coordinator.async_refresh()

    async def async_dashboard_yaml(self) -> dict[str, str]:
        """Return a ready-to-paste Lovelace YAML block for this vacuum.

        Resolves the vacuum's own entity ID, the floor-plan image entity ID
        (when this device has MARD geometry), every preset button, every
        room-select switch, and the Clean-selected-rooms button, then
        renders a single ``vertical-stack`` card the user can paste.

        Entity IDs are looked up via the registry rather than constructed from
        names, because user-renamed entities keep their unique_id but get a
        new ``entity_id`` — the registry is the only source that survives
        renames. Filtering by the registry's config_entry_id keeps multi-
        vacuum households from cross-contaminating each other's cards.
        """
        registry = er.async_get(self.hass)
        serial = self.sharkiq.serial_number
        entry_id = self.coordinator.config_entry.entry_id

        image_entity_id = registry.async_get_entity_id(
            IMAGE_DOMAIN, DOMAIN, f"{serial}_floor_plan"
        )

        preset_entity_ids: list[str] = []
        for preset in self.coordinator.config_entry.options.get(CONF_PRESETS, []):
            if preset.get(PRESET_SERIAL) != serial:
                continue
            preset_id = preset.get(PRESET_ID) or preset.get(PRESET_NAME, "preset")
            entity_id = registry.async_get_entity_id(
                "button", DOMAIN, f"{serial}_preset_{preset_id}"
            )
            if entity_id is not None:
                preset_entity_ids.append(entity_id)

        # Pull the per-room select switches off the registry and sort by
        # entity_id so the dashboard ordering is stable across HA restarts —
        # the underlying MARD list ordering is too, but registry iteration
        # is not guaranteed to follow it.
        select_switch_entity_ids: list[str] = []
        for entry in er.async_entries_for_config_entry(registry, entry_id):
            if entry.domain != "switch":
                continue
            unique_id = entry.unique_id or ""
            if unique_id.startswith(f"{serial}_select_"):
                select_switch_entity_ids.append(entry.entity_id)
        select_switch_entity_ids.sort()

        return {
            "dashboard_yaml": build_vacuum_dashboard_yaml(
                self.entity_id,
                image_entity_id,
                preset_entity_ids,
                room_select_switch_entity_ids=select_switch_entity_ids,
            )
        }

    @property
    def fan_speed(self) -> str | None:
        """Return the current fan speed."""
        fan_speed = None
        speed_level = self.sharkiq.get_property_value(Properties.POWER_MODE)
        for k, val in FAN_SPEEDS_MAP.items():
            if val == speed_level:
                fan_speed = k
        return fan_speed

    async def async_set_fan_speed(self, fan_speed: str, **kwargs: Any) -> None:
        """Set the fan speed.

        Skegox shadows are eventually-consistent: a synchronous refresh
        right after the PATCH would re-read the still-old reported value
        and snap the UI back to it (the visible bug a user reported as
        "only Normal is selectable"). The wrapper updates its local state
        optimistically inside ``async_set_property_value``, so push that
        to HA directly and let the next regular poll reconcile.
        """
        normalized = fan_speed.capitalize()
        if normalized not in FAN_SPEEDS_MAP:
            return
        await self.sharkiq.async_set_property_value(
            Properties.POWER_MODE, FAN_SPEEDS_MAP[normalized]
        )
        self.async_write_ha_state()

    # Various attributes we want to expose
    @property
    def recharge_resume(self) -> bool | None:
        """Recharge and resume mode active."""
        return self.sharkiq.get_property_value(Properties.RECHARGE_RESUME)

    @property
    def rssi(self) -> int | None:
        """Get the WiFi RSSI."""
        return self.sharkiq.get_property_value(Properties.RSSI)

    @property
    def low_light(self):
        """Let us know if the robot is operating in low-light mode."""
        return self.sharkiq.get_property_value(Properties.LOW_LIGHT_MISSION)

    @property
    def available_rooms(self) -> list | None:
        """Return a list of rooms available to clean.

        Skegox devices with a parsed MARD expose ``display_rooms`` whose keys
        are the SharkClean app's ``user_room_name`` values (post-merge,
        post-rename). Those are what the user expects to see; only fall back
        to the shadow's ``Robot_Room_List`` when the MARD path isn't
        available (legacy Ayla, or MARD load failed at setup).
        """
        display_rooms = getattr(self.sharkiq, "display_rooms", None)
        if display_rooms:
            return list(display_rooms.keys())
        room_list = self.sharkiq.get_property_value(Properties.ROBOT_ROOM_LIST)
        if room_list:
            return room_list.split(":")[1:]
        return []

    @property
    def schedule(self) -> dict[str, Any] | None:
        """Return the on-device weekly schedule (newer skegox devices only).

        Read-only; HA-side automations should call ``sharkiq.clean_room`` on
        a time trigger for room scheduling rather than mutating the on-device
        schedule. Older Ayla devices don't expose Schedule, and the upstream
        sharkiq library raises KeyError on missing keys, so swallow that.
        """
        try:
            return self.sharkiq.get_property_value("Schedule")
        except KeyError:
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return a dictionary of device state attributes specific to sharkiq."""
        return {
            ATTR_ERROR_CODE: self.error_code,
            ATTR_ERROR_MSG: self.sharkiq.error_text,
            ATTR_LOW_LIGHT: self.low_light,
            ATTR_RECHARGE_RESUME: self.recharge_resume,
            ATTR_ROOMS: self.available_rooms,
            ATTR_SCHEDULE: self.schedule,
        }
