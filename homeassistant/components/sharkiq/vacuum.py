"""Shark IQ Wrapper."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from sharkiq import OperatingModes, PowerModes, Properties

from homeassistant.components.vacuum import (
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import ATTR_ROOMS, DOMAIN, LOGGER, SHARK
from .coordinator import SharkDevice, SharkIqConfigEntry, SharkIqUpdateCoordinator

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
        """Start the device."""
        await self.sharkiq.async_set_operating_mode(OperatingModes.START)
        await self.coordinator.async_refresh()

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
        """Return a list of rooms available to clean."""
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
