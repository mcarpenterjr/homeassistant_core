"""Tests for the Shark IQ floor-plan image entity."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from homeassistant.components.sharkiq.image import SharkIqFloorPlanImage
from homeassistant.components.sharkiq.skegox import SkegoxDevice
from homeassistant.core import HomeAssistant

from .test_skegox import SKEGOX_DEVICE_DATA, MockSkegoxApi


def _make_device_with_mard_areas(areas: list[dict[str, Any]]) -> SkegoxDevice:
    """Stand up a SkegoxDevice with pre-populated MARD geometry.

    Bypasses the network-bound MARD fetcher because the image entity's
    contract is "render whatever's on ``mard_areas`` right now" — fetching
    is tested separately in test_skegox.py.
    """
    device = SkegoxDevice(MockSkegoxApi(), "household", deepcopy(SKEGOX_DEVICE_DATA))
    device._mard_areas = areas  # type: ignore[attr-defined]
    return device


def test_floor_plan_image_returns_svg_bytes(hass: HomeAssistant) -> None:
    """``image()`` returns valid SVG bytes when MARD geometry is present."""
    device = _make_device_with_mard_areas(
        [
            {
                "display_name": "Kitchen",
                "uuid": "u-kitchen",
                "points": [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)],
            }
        ]
    )

    entity = SharkIqFloorPlanImage(hass, device, _DummyCoordinator())
    raw = entity.image()

    assert isinstance(raw, bytes)
    decoded = raw.decode()
    assert decoded.startswith("<?xml")
    assert "<polygon" in decoded
    assert ">Kitchen<" in decoded
    assert entity.content_type == "image/svg+xml"


def test_floor_plan_image_returns_none_when_no_geometry(hass: HomeAssistant) -> None:
    """Without MARD areas, ``image()`` returns ``None`` (no entity should
    have been created in that case — but the runtime contract still holds)."""
    device = _make_device_with_mard_areas([])
    device._mard_areas = None  # type: ignore[attr-defined]

    entity = SharkIqFloorPlanImage(hass, device, _DummyCoordinator())

    assert entity.image() is None


def test_floor_plan_image_signature_picks_up_renames(hass: HomeAssistant) -> None:
    """Renaming a room bumps ``image_last_updated`` so the frontend reloads.

    Without this, the browser caches the previous SVG and the user keeps
    seeing old labels for hours until the entity ID's access token cycles.
    """
    device = _make_device_with_mard_areas(
        [
            {
                "display_name": "Old Name",
                "uuid": "u1",
                "points": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)],
            }
        ]
    )
    entity = SharkIqFloorPlanImage(hass, device, _DummyCoordinator())
    first_ts = entity.image_last_updated

    device._mard_areas = [  # type: ignore[attr-defined]
        {
            "display_name": "New Name",
            "uuid": "u1",
            "points": [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)],
        }
    ]
    # Use the change-detection helper directly so the test doesn't need a
    # fully wired HA state machine (which the public coordinator update
    # callback requires via ``async_write_ha_state``).
    assert entity._refresh_if_areas_changed() is True

    assert entity.image_last_updated is not None
    assert entity.image_last_updated != first_ts
    # A second call with no further changes does NOT bump the timestamp.
    second_ts = entity.image_last_updated
    assert entity._refresh_if_areas_changed() is False
    assert entity.image_last_updated == second_ts


class _DummyCoordinator:
    """Minimal stand-in for SharkIqUpdateCoordinator.

    The image entity inherits from ``CoordinatorEntity`` which only needs
    ``last_update_success`` and ``async_add_listener`` from its coordinator
    to be constructed; the actual update plumbing isn't exercised here.
    """

    last_update_success = True

    def async_add_listener(self, *_args: Any, **_kwargs: Any) -> Any:
        """No-op listener registration."""
        return lambda: None
