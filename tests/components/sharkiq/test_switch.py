"""Tests for the per-room select switches in the Shark IQ integration."""

from __future__ import annotations

from copy import deepcopy

import pytest

from homeassistant.components.sharkiq.skegox import SkegoxDevice
from homeassistant.components.sharkiq.switch import SharkRoomSelectSwitch
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, State

from .test_skegox import SKEGOX_DEVICE_DATA, MockSkegoxApi


class _DummyCoordinator:
    """Stand-in for SharkIqUpdateCoordinator (CoordinatorEntity bootstrap only)."""

    last_update_success = True

    def async_add_listener(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return lambda: None


def _make_switch(room_name: str) -> SharkRoomSelectSwitch:
    device = SkegoxDevice(MockSkegoxApi(), "household", deepcopy(SKEGOX_DEVICE_DATA))
    return SharkRoomSelectSwitch(device, _DummyCoordinator(), room_name)


def test_select_switch_unique_id_uses_room_slug() -> None:
    """unique_id encodes the slug so renames in HA don't break the binding.

    Slugify handles punctuation and case: "Tom's Office!" becomes a stable
    identifier the registry can survive. Device serial prefix ensures
    multi-vacuum households don't collide.
    """
    switch = _make_switch("Master Bedroom")
    assert switch.unique_id == "AC000Wxxxxxxxxx_select_master_bedroom"


def test_select_switch_name_and_room_property() -> None:
    """Entity name is the bare room; the device name in DeviceInfo
    prefixes it on the device card."""
    switch = _make_switch("Kitchen")
    assert switch.name == "Kitchen"
    assert switch.room_name == "Kitchen"


def test_select_switch_starts_off() -> None:
    """A freshly-created switch is off until the user toggles it on."""
    switch = _make_switch("Den")
    assert switch.is_on is False


async def test_select_switch_turn_on_off(hass: HomeAssistant) -> None:
    """``async_turn_on`` / ``async_turn_off`` update the in-memory state."""
    switch = _make_switch("Foyer")
    switch.hass = hass
    switch.entity_id = "switch.sharknado_select_foyer"

    await switch.async_turn_on()
    assert switch.is_on is True
    await switch.async_turn_off()
    assert switch.is_on is False


@pytest.mark.parametrize("restored, expected_on", [(STATE_ON, True), (STATE_OFF, False)])
async def test_select_switch_restores_state(
    hass: HomeAssistant,
    restored: str,
    expected_on: bool,
) -> None:
    """RestoreEntity carries the user's selection across HA restarts."""
    switch = _make_switch("Bedroom")
    switch.hass = hass
    switch.entity_id = "switch.sharknado_select_bedroom"

    async def _fake_last_state() -> State:
        return State(switch.entity_id, restored)

    switch.async_get_last_state = _fake_last_state  # type: ignore[method-assign]
    await switch.async_added_to_hass()

    assert switch.is_on is expected_on
