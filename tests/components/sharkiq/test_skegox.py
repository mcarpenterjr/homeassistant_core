"""Test the Shark IQ skegox backend."""

from __future__ import annotations

from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.components.sharkiq.auth import SharkAuth
from homeassistant.components.sharkiq.const import ATTR_ROOMS, CONF_BACKEND, DOMAIN
from homeassistant.components.sharkiq.coordinator import SharkIqUpdateCoordinator
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
from homeassistant.const import (
    ATTR_SUPPORTED_FEATURES,
    CONF_PASSWORD,
    CONF_REGION,
    CONF_USERNAME,
)
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

TEST_USERNAME = "test-username"
TEST_PASSWORD = "test-password"
TEST_REGION = "elsewhere"
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
        self._refresh_token = "fake_refresh_token"
        self._token_expiry = 9999999999.0
        self._username = TEST_USERNAME
        self._password = TEST_PASSWORD
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
    """Build the mock skegox integration."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TEST_USERNAME,
        data={
            CONF_USERNAME: TEST_USERNAME,
            CONF_PASSWORD: TEST_PASSWORD,
            CONF_REGION: TEST_REGION,
            CONF_BACKEND: "skegox",
        },
        entry_id=ENTRY_ID,
    )
    entry.add_to_hass(hass)

    mock_api = MockSkegoxApi()

    with (
        patch(
            "homeassistant.components.sharkiq._async_setup_skegox",
        ) as mock_setup_skegox,
        patch(
            "homeassistant.components.sharkiq._async_setup_ayla",
            return_value=None,
        ),
    ):
        devices = await mock_api.async_get_devices()
        coordinator = SharkIqUpdateCoordinator(
            hass,
            entry,
            shark_vacs=devices,
            skegox_api=mock_api,
        )
        mock_setup_skegox.return_value = coordinator

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
