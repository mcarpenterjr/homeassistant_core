"""Test the Shark IQ skegox backend."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.components.sharkiq.auth import SharkAuth
from homeassistant.components.sharkiq.const import (
    ATTR_ROOMS,
    BACKEND_SKEGOX,
    CONF_BACKEND,
    CONF_ID_TOKEN,
    CONF_REFRESH_TOKEN,
    CONF_TOKEN_EXPIRY,
    DOMAIN,
)
from homeassistant.components.sharkiq.skegox import SkegoxApi, SkegoxDevice
from homeassistant.components.sharkiq.vacuum import (
    ATTR_ERROR_CODE,
    ATTR_ERROR_MSG,
    ATTR_LOW_LIGHT,
    ATTR_RECHARGE_RESUME,
    FAN_SPEEDS_MAP,
)
from homeassistant.components.vacuum import (
    ATTR_BATTERY_LEVEL,
    ATTR_FAN_SPEED_LIST,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.const import ATTR_SUPPORTED_FEATURES, CONF_REGION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from tests.common import MockConfigEntry

SKEGOX_DEVICE_DATA: dict[str, Any] = {
    "snd": "AC000Wxxxxxxxxx",
    "registry": {
        "name": "Sharknado",
        "model": "AY001MRT1",
        "oemModel": "RV1000A",
        "metadata": {
            "vacModelNumber": "RV1001AE",
            "vacSerialNumber": "S26xxxxxxxxx",
        },
    },
    "connectivityStatus": {
        "connected": True,
    },
    "shadow": {
        "properties": {
            "reported": {
                "Battery_Capacity": 50,
                "Charging_Status": 0,
                "CleanComplete": 0,
                "DockedStatus": 0,
                "Error_Code": 7,
                "Evacuating": 1,
                "Find_Device": 0,
                "LowLightMission": 0,
                "Operating_Mode": 2,
                "Power_Mode": 1,
                "RSSI": -46,
                "Recharge_Resume": 1,
                "Recharging_To_Resume": 0,
                "Robot_Firmware_Version": "Dummy Firmware 1.0",
                "Robot_Room_List": "AY001MRT1:Kitchen:Living Room",
            }
        }
    },
}

TEST_REGION = "elsewhere"
TEST_REFRESH_TOKEN = "test-refresh-token"
TEST_ID_TOKEN = "test-id-token"
ENTRY_ID = "skegox_entry_0123456789abcdef"
VAC_ENTITY_ID = "vacuum.sharknado"
ROOM_LIST = ["Kitchen", "Living Room"]

EXPECTED_FEATURES = (
    VacuumEntityFeature.BATTERY
    | VacuumEntityFeature.FAN_SPEED
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.START
    | VacuumEntityFeature.STATE
    | VacuumEntityFeature.STOP
    | VacuumEntityFeature.LOCATE
)


class MockSkegoxAuth(SharkAuth):
    """Mocked SharkAuth that doesn't hit the network."""

    def __init__(self) -> None:
        """Initialize without calling super."""
        self._id_token = "fake.eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhdXRoMHwxMjM0NTY3ODkwIiwiZW1haWwiOiJ0ZXN0QGV4YW1wbGUuY29tIn0.fake"
        self._access_token = "fake_access_token"
        self._refresh_token = TEST_REFRESH_TOKEN
        self._token_expiry = 9_999_999_999.0
        self._europe = False

    @property
    def id_token(self) -> str | None:
        """Return fake id_token."""
        return self._id_token

    @property
    def user_id(self) -> str | None:
        """Return fake user_id."""
        return "1234567890"

    @property
    def token_expiring_soon(self) -> bool:
        """Token is never expiring in tests."""
        return False

    @property
    def is_authenticated(self) -> bool:
        """Always authenticated in tests."""
        return True

    async def async_sign_in(self) -> None:
        """Don't actually sign in."""

    async def async_refresh_auth(self) -> None:
        """Don't actually refresh."""


class MockSkegoxApi(SkegoxApi):
    """Mocked SkegoxApi that returns test data."""

    def __init__(self) -> None:
        """Initialize without calling super."""
        self._auth = MockSkegoxAuth()
        self._household_id = "test-household-id"
        self._devices_data = [deepcopy(SKEGOX_DEVICE_DATA)]

    @property
    def auth(self) -> SharkAuth:
        """Return mock auth."""
        return self._auth

    async def async_get_household_id(self) -> str:
        """Return test household ID."""
        return self._household_id

    async def async_get_devices(self) -> list[SkegoxDevice]:
        """Return test devices."""
        return [
            SkegoxDevice(self, self._household_id, data)
            for data in self._devices_data
        ]

    async def async_list_devices(self) -> list[dict[str, Any]]:
        """Return device list with connection status."""
        return [
            {
                "dsn": data.get("snd", ""),
                "connection_status": "Online"
                if data.get("connectivityStatus", {}).get("connected", False)
                else "Offline",
            }
            for data in self._devices_data
        ]

    async def async_get_device_state(
        self, household_id: str, device_snd: str
    ) -> dict[str, Any]:
        """Return test device state."""
        for data in self._devices_data:
            if data.get("snd") == device_snd:
                return data
        return {}

    async def async_set_device_property(
        self, household_id: str, device_snd: str, prop_name: str, value: Any
    ) -> None:
        """Set property locally."""
        for data in self._devices_data:
            if data.get("snd") == device_snd:
                data["shadow"]["properties"]["reported"][prop_name] = value


@pytest.fixture(autouse=True)
async def setup_skegox_integration(hass: HomeAssistant) -> None:
    """Build the mock skegox integration.

    The fixture mocks at the boundary (SharkAuth refresh, SkegoxApi device
    list) rather than at the integration's internal helpers — that lets
    the real ``async_setup_entry`` run end to end and exercises the new
    PKCE token-restore code path.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="auth0|user-sub",
        version=2,
        data={
            CONF_REGION: TEST_REGION,
            CONF_REFRESH_TOKEN: TEST_REFRESH_TOKEN,
            CONF_ID_TOKEN: TEST_ID_TOKEN,
            # In the future — refresh-on-startup is skipped when not
            # expiring soon, so the test fixture doesn't have to mock the
            # network call.
            CONF_TOKEN_EXPIRY: 9_999_999_999.0,
            CONF_BACKEND: BACKEND_SKEGOX,
        },
        entry_id=ENTRY_ID,
    )
    entry.add_to_hass(hass)

    mock_api = MockSkegoxApi()

    with (
        patch(
            "homeassistant.components.sharkiq.SkegoxApi",
            return_value=mock_api,
        ),
        patch(
            "homeassistant.components.sharkiq.SharkAuth.async_refresh_auth",
        ),
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()


async def test_skegox_simple_properties(
    hass: HomeAssistant, entity_registry: er.EntityRegistry
) -> None:
    """Test that simple properties work with skegox backend."""
    state = hass.states.get(VAC_ENTITY_ID)
    entity = entity_registry.async_get(VAC_ENTITY_ID)

    assert entity
    assert state
    assert state.state == VacuumActivity.CLEANING
    assert entity.unique_id == "AC000Wxxxxxxxxx"


@pytest.mark.parametrize(
    ("attribute", "target_value"),
    [
        (ATTR_SUPPORTED_FEATURES, EXPECTED_FEATURES),
        (ATTR_BATTERY_LEVEL, 50),
        (ATTR_FAN_SPEED_LIST, list(FAN_SPEEDS_MAP)),
        (ATTR_ERROR_CODE, 7),
        (ATTR_ERROR_MSG, "Cliff sensor is blocked"),
        (ATTR_LOW_LIGHT, 0),
        (ATTR_RECHARGE_RESUME, 1),
        (ATTR_ROOMS, ROOM_LIST),
    ],
)
async def test_skegox_initial_attributes(
    hass: HomeAssistant, attribute: str, target_value: Any
) -> None:
    """Test initial attributes with skegox backend."""
    state = hass.states.get(VAC_ENTITY_ID)
    assert state.attributes.get(attribute) == target_value


@pytest.mark.parametrize(
    ("device_property", "target_value"),
    [
        ("manufacturer", "Shark"),
        ("model", "RV1001AE"),
        ("name", "Sharknado"),
        ("sw_version", "Dummy Firmware 1.0"),
    ],
)
async def test_skegox_device_properties(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    device_property: str,
    target_value: str,
) -> None:
    """Test device properties with skegox backend."""
    device = device_registry.async_get_device(identifiers={(DOMAIN, "AC000Wxxxxxxxxx")})
    assert getattr(device, device_property) == target_value


async def test_skegox_error_text() -> None:
    """Test SkegoxDevice error text mapping."""
    mock_api = MockSkegoxApi()
    device = SkegoxDevice(mock_api, "household", deepcopy(SKEGOX_DEVICE_DATA))

    assert device.error_code == 7
    assert device.error_text == "Cliff sensor is blocked"

    # Test no error
    device.properties_full["Error_Code"]["value"] = 0
    assert device.error_text is None

    # Test unknown error
    device.properties_full["Error_Code"]["value"] = 999
    assert device.error_text == "Unknown error (999)"


def _make_device_with_mard_fetcher(mard_body: Any) -> SkegoxDevice:
    """Build a SkegoxDevice whose API's ``fetch_property_file`` returns the given body."""
    mock_api = MockSkegoxApi()

    async def _fake_fetch(
        household_id: str, device_snd: str, property_name: str
    ) -> Any:
        assert property_name == "MARD"
        return mard_body

    mock_api.async_fetch_property_file = _fake_fetch  # type: ignore[method-assign]
    return SkegoxDevice(mock_api, "household", deepcopy(SKEGOX_DEVICE_DATA))


async def test_skegox_load_mard_builds_display_rooms() -> None:
    """Happy path: MARD with simple distinct rooms populates display_rooms."""
    device = _make_device_with_mard_fetcher(
        {
            "floor_id": "FLOOR_1",
            "areas": [
                {"user_room_name": "Kitchen", "robot_room_name": "AZ_1"},
                {"user_room_name": "Living Room", "robot_room_name": "AZ_2"},
            ],
        }
    )

    await device.async_load_mard()

    assert device.display_rooms == {
        "Kitchen": ["AZ_1"],
        "Living Room": ["AZ_2"],
    }


async def test_skegox_load_mard_merges_shared_user_room_names() -> None:
    """Merged areas share one ``user_room_name`` mapped to all robot names.

    This is the bug behind "rooms in HA don't match the app" — the SharkClean
    app collapses merged areas into a single tile labelled with the user-typed
    name, while the shadow's ``Robot_Room_List`` keeps the underlying
    ``AZ_N`` rows. MARD is what makes the two views agree.
    """
    device = _make_device_with_mard_fetcher(
        {
            "floor_id": "FLOOR_1",
            "areas": [
                {"user_room_name": "Kitchen", "robot_room_name": "AZ_3"},
                {"user_room_name": "Kitchen", "robot_room_name": "AZ_4"},
                {"user_room_name": "Hallway", "robot_room_name": "AZ_5"},
            ],
        }
    )

    await device.async_load_mard()

    assert device.display_rooms == {
        "Kitchen": ["AZ_3", "AZ_4"],
        "Hallway": ["AZ_5"],
    }


async def test_skegox_load_mard_fetch_failure_falls_back() -> None:
    """A failed MARD fetch leaves display_rooms unset so callers fall back."""
    mock_api = MockSkegoxApi()

    async def _failing_fetch(
        household_id: str, device_snd: str, property_name: str
    ) -> Any:
        from homeassistant.components.sharkiq.skegox import SkegoxApiError

        raise SkegoxApiError("no MARD here")

    mock_api.async_fetch_property_file = _failing_fetch  # type: ignore[method-assign]
    device = SkegoxDevice(mock_api, "household", deepcopy(SKEGOX_DEVICE_DATA))

    await device.async_load_mard()

    assert device.display_rooms is None


@pytest.mark.parametrize(
    "body",
    [
        "not a dict",
        {"floor_id": "X"},  # no areas key
        {"floor_id": "X", "areas": "not a list"},
        {"floor_id": "X", "areas": []},
        {"floor_id": "X", "areas": [{"missing": "fields"}]},
        # Robot name empty means we genuinely have nothing to anchor the
        # dropdown label to; that entry is skipped.
        {
            "floor_id": "X",
            "areas": [{"user_room_name": "Real", "robot_room_name": ""}],
        },
    ],
)
async def test_skegox_load_mard_malformed_body_leaves_display_rooms_unset(
    body: Any,
) -> None:
    """Defensive: surprising MARD shapes never blow up setup."""
    device = _make_device_with_mard_fetcher(body)

    await device.async_load_mard()

    assert device.display_rooms is None


async def test_skegox_load_mard_uses_robot_room_name_when_user_name_empty() -> (
    None
):
    """SharkClean only fills ``user_room_name`` when the user has renamed a
    room — auto-assigned rooms ship with that field empty and the display
    label sitting in ``robot_room_name``. Real bodies observed in the wild
    are mostly this shape; if we strictly require ``user_room_name`` the
    HA dropdown shows just the single renamed room.
    """
    device = _make_device_with_mard_fetcher(
        {
            "floor_id": "FLOOR_1",
            "areas": [
                # User-renamed: takes user_room_name.
                {
                    "robot_room_name": "AZ_1",
                    "user_room_name": "Laundry Room",
                },
                # Auto-assigned: empty user_room_name, falls back to robot name.
                {"robot_room_name": "Bedroom", "user_room_name": ""},
                {"robot_room_name": "Foyer"},  # field missing entirely
            ],
        }
    )

    await device.async_load_mard()

    assert device.display_rooms == {
        "Laundry Room": ["AZ_1"],
        "Bedroom": ["Bedroom"],
        "Foyer": ["Foyer"],
    }


def test_skegox_expand_display_rooms_passthrough_without_mard() -> None:
    """Without a MARD mapping, the room list is returned unchanged."""
    device = SkegoxDevice(MockSkegoxApi(), "household", deepcopy(SKEGOX_DEVICE_DATA))

    assert device._expand_display_rooms(["Kitchen", "Foyer"]) == ["Kitchen", "Foyer"]


def test_skegox_expand_display_rooms_resolves_merges_and_dedupes() -> None:
    """Display names expand; unknowns pass through; duplicates collapse.

    Passing both a display name and one of its underlying robot names would
    naively send the same robot name twice — that's a payload that fails on
    some firmware revisions, so the expander deduplicates.
    """
    device = SkegoxDevice(MockSkegoxApi(), "household", deepcopy(SKEGOX_DEVICE_DATA))
    device._display_rooms = {
        "Kitchen": ["AZ_3", "AZ_4"],
        "Foyer": ["AZ_5"],
    }

    assert device._expand_display_rooms(["Kitchen"]) == ["AZ_3", "AZ_4"]
    assert device._expand_display_rooms(["Kitchen", "Foyer"]) == [
        "AZ_3",
        "AZ_4",
        "AZ_5",
    ]
    # Unknown display name (e.g. user passed a raw robot name): pass-through.
    assert device._expand_display_rooms(["AZ_9"]) == ["AZ_9"]
    # Display name + one of its underlying names: deduped.
    assert device._expand_display_rooms(["Kitchen", "AZ_3"]) == [
        "AZ_3",
        "AZ_4",
    ]


async def test_skegox_load_mard_caches_polygon_geometry() -> None:
    """``mard_areas`` is populated with per-area geometry the renderer needs.

    Coordinates from MARD are floats; the renderer needs ``(float, float)``
    tuples and at least three points per polygon. Degenerate areas (fewer
    than 3 points) are dropped so the image entity never has to defend
    against them.
    """
    device = _make_device_with_mard_fetcher(
        {
            "floor_id": "F1",
            "areas": [
                {
                    "robot_room_name": "Kitchen",
                    "user_room_name": "",
                    "uuid": "u-kitchen",
                    "points": [
                        {"x": 0, "y": 0},
                        {"x": 10, "y": 0},
                        {"x": 10, "y": 10},
                        {"x": 0, "y": 10},
                    ],
                },
                {
                    "robot_room_name": "Foyer",
                    "user_room_name": "",
                    "uuid": "u-foyer",
                    "points": [{"x": 1, "y": 1}],  # too few points, dropped
                },
            ],
        }
    )

    await device.async_load_mard()

    assert device.mard_areas is not None
    assert [a["display_name"] for a in device.mard_areas] == ["Kitchen"]
    kitchen = device.mard_areas[0]
    assert kitchen["uuid"] == "u-kitchen"
    assert kitchen["points"] == [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 10.0),
        (0.0, 10.0),
    ]


async def test_skegox_clean_rooms_uses_mard_floor_id_and_expansion() -> None:
    """Clean payload uses the MARD floor_id and expands merged display rooms."""
    device = SkegoxDevice(MockSkegoxApi(), "household", deepcopy(SKEGOX_DEVICE_DATA))
    device._display_rooms = {"Kitchen": ["AZ_3", "AZ_4"]}
    device._mard_floor_id = "FLOOR_MARD"
    # Force V3 payload path — newer devices use it exclusively.
    device.properties_full["AreasToClean_V3"] = {
        "value": "",
        "read_only": False,
        "base_type": "str",
    }

    captured: dict[str, Any] = {}

    async def _capture(property_name: Any, value: Any) -> None:
        captured.setdefault(property_name, value)

    device.async_set_property_value = _capture  # type: ignore[method-assign]

    await device.async_clean_rooms(["Kitchen"])

    import json as _json

    payload = _json.loads(captured["AreasToClean_V3"])
    assert payload["floor_id"] == "FLOOR_MARD"
    assert payload["areas_to_clean"] == {"UserRoom": ["AZ_3", "AZ_4"]}
