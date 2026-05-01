"""Skegox API client for SharkNinja devices (new backend)."""

from __future__ import annotations

import asyncio
import enum
import json
import secrets
import time
from typing import Any

import aiohttp

from .auth import SharkAuth, SharkAuthError, SharkAuthExpiredError
from .const import (
    LOGGER,
    SKEGOX_API_KEY_EU,
    SKEGOX_API_KEY_US,
    SKEGOX_BASE_URL_EU,
    SKEGOX_BASE_URL_US,
    SKEGOX_CALLER,
)

# Error code to message mapping (matches sharkiq library)
ERROR_MESSAGES: dict[int, str] = {
    0: "No error",
    1: "Side wheel is stuck",
    2: "Side brush is stuck",
    3: "Suction motor has a problem",
    4: "Brushroll is stuck",
    5: "Robot is stuck with no forward motion",
    6: "Robot is stuck with a bump condition",
    7: "Cliff sensor is blocked",
    8: "Battery power is low",
    9: "No dustbin is installed",
    10: "Fall sensor is stuck",
    11: "Front bumper is stuck",
    12: "Robot is stuck on a raised surface",
    13: "Robot has been picked up",
    14: "No path to drive",
    15: "Robot is stuck near a wall or corner",
    16: "Side wheel is stuck (2)",
    17: "Robot is stuck near a wall or corner (2)",
    18: "Self-empty base is clogged",
}


class SkegoxApiError(Exception):
    """Error communicating with skegox API."""


class SkegoxApi:
    """Client for the SharkNinja skegox API (thor.skegox.com)."""

    def __init__(
        self,
        auth: SharkAuth,
        websession: aiohttp.ClientSession,
        *,
        europe: bool = False,
    ) -> None:
        """Initialize the skegox API client."""
        self._auth = auth
        self._websession = websession
        self._base_url = SKEGOX_BASE_URL_EU if europe else SKEGOX_BASE_URL_US
        self._api_key = SKEGOX_API_KEY_EU if europe else SKEGOX_API_KEY_US
        self._household_id: str | None = None

    @property
    def auth(self) -> SharkAuth:
        """Return the auth handler."""
        return self._auth

    def _headers(self) -> dict[str, str]:
        """Build request headers with fake HMAC signature."""
        if not self._auth.id_token:
            raise SharkAuthExpiredError("No id_token available")

        now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        return {
            "Authorization": f"Bearer {self._auth.id_token}",
            "content-type": "application/json",
            "x-api-key": self._api_key,
            "x-iotn-request-signature": self._auth.build_signature_header(now),
            "x-iotn-caller": SKEGOX_CALLER,
            "x-sn-nonce": secrets.token_hex(16),
            "x-sn-date": now,
        }

    async def _async_request(
        self,
        method: str,
        path: str,
        *,
        json_data: dict | None = None,
        params: dict[str, str] | None = None,
    ) -> Any:
        """Make an authenticated request to the skegox API.

        SharkNinja's skegox endpoints resolve to a small AWS pool, and we've
        observed individual edge IPs go through periods of returning fast
        connect failures while the others are healthy. Retry once on
        ClientConnectorError so a transient bad pick doesn't take the whole
        coordinator update down.
        """
        url = f"{self._base_url}{path}"
        last_conn_err: aiohttp.ClientConnectorError | None = None
        for attempt in range(2):
            try:
                async with self._websession.request(
                    method,
                    url,
                    headers=self._headers(),
                    json=json_data,
                    params=params,
                ) as resp:
                    if resp.status == 401:
                        raise SharkAuthExpiredError("Skegox API returned 401")
                    if resp.status == 403:
                        raise SharkAuthError("Skegox API returned 403 forbidden")
                    if resp.status >= 400:
                        body = await resp.text()
                        raise SkegoxApiError(
                            f"Skegox API returned {resp.status}: {body}"
                        )
                    return await resp.json()
            except aiohttp.ClientConnectorError as err:
                last_conn_err = err
                if attempt == 0:
                    LOGGER.debug(
                        "Skegox connect failed, retrying once: %s", err
                    )
                    await asyncio.sleep(0.5)
                    continue
                raise SkegoxApiError(f"Network error: {err}") from err
            except aiohttp.ClientError as err:
                raise SkegoxApiError(f"Network error: {err}") from err
        # Defensive — loop only exits via return or raise above.
        raise SkegoxApiError(f"Network error: {last_conn_err}")

    async def async_get_household_id(self) -> str:
        """Get the household ID for the authenticated user."""
        if self._household_id:
            return self._household_id

        user_id = self._auth.user_id
        if not user_id:
            raise SkegoxApiError("Cannot determine user_id from token")

        result = await self._async_request(
            "GET",
            "/householdsEndUser",
            params={"userId": user_id},
        )
        households = result.get("households", [])
        if not households:
            raise SkegoxApiError("No households found for user")

        self._household_id = households[0]
        if len(households) > 1:
            LOGGER.debug(
                "Found %d households, using first: %s",
                len(households),
                self._household_id,
            )
        else:
            LOGGER.debug("Found household ID: %s", self._household_id)
        return self._household_id

    async def async_get_devices(self) -> list[SkegoxDevice]:
        """Get all devices from the skegox API."""
        household_id = await self.async_get_household_id()
        user_id = self._auth.user_id

        result = await self._async_request(
            "GET",
            f"/devicesEndUserController/{household_id}/users/{user_id}",
            params={
                "includeRegistry": "true",
                "includeConnectivityStatus": "true",
                "includeShadow": "true",
            },
        )

        devices_data = (
            result
            if isinstance(result, list)
            else result.get("items") or result.get("devices") or []
        )
        devices = []
        for device_data in devices_data:
            device = SkegoxDevice(self, household_id, device_data)
            devices.append(device)

        LOGGER.debug("Found %d skegox device(s)", len(devices))
        return devices

    async def async_list_devices(self) -> list[dict[str, Any]]:
        """List devices with connection status (compatible with Ayla format)."""
        household_id = await self.async_get_household_id()
        user_id = self._auth.user_id

        result = await self._async_request(
            "GET",
            f"/devicesEndUserController/{household_id}/users/{user_id}",
            params={
                "includeConnectivityStatus": "true",
                "includeShadow": "true",
            },
        )

        devices_data = (
            result
            if isinstance(result, list)
            else result.get("items") or result.get("devices") or []
        )
        device_list = []
        for dev in devices_data:
            conn = dev.get("connectivityStatus", {})
            dsn = dev.get("deviceId") or dev.get("snd") or dev.get("dsn", "")
            connected = conn.get("connected", False)
            device_list.append(
                {
                    "dsn": dsn,
                    "connection_status": "Online" if connected else "Offline",
                }
            )
        return device_list

    async def async_get_device_state(
        self, household_id: str, device_snd: str
    ) -> dict[str, Any]:
        """Get full device state from skegox."""
        return await self._async_request(
            "GET",
            f"/devicesEndUserController/{household_id}/devices/{device_snd}",
        )

    async def async_set_device_property(
        self, household_id: str, device_snd: str, prop_name: str, value: Any
    ) -> None:
        """Set a device property via PATCH."""
        await self._async_request(
            "PATCH",
            f"/devicesEndUserController/{household_id}/devices/{device_snd}",
            json_data={
                "shadow": {
                    "properties": {
                        "desired": {
                            prop_name: value,
                        }
                    }
                }
            },
        )


class SkegoxDevice:
    """Device wrapper for skegox backend, compatible with SharkIqVacuum interface."""

    def __init__(
        self,
        api: SkegoxApi,
        household_id: str,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize a skegox device."""
        self._api = api
        self._household_id = household_id
        self._device_data = device_data

        # Extract identity info — supports both schemas:
        # - Older: top-level "snd"/"dsn", registry.name/model/oemModel
        # - Newer (RV2500AX et al.): top-level "deviceId", metadata.deviceName,
        #   registry.modelNumber / registry.Device_Model_Number
        self._snd = (
            device_data.get("deviceId")
            or device_data.get("snd")
            or device_data.get("dsn", "")
        )
        metadata = device_data.get("metadata", {})
        registry = device_data.get("registry", {})
        self._name = (
            metadata.get("deviceName")
            or registry.get("name")
            or "Shark Vacuum"
        )
        self._model = (
            registry.get("modelNumber")
            or registry.get("model")
            or ""
        )
        self._oem_model = (
            registry.get("Device_Model_Number")
            or registry.get("oemModel")
            or self._model
        )

        # Build properties dict from shadow data
        self.properties_full: dict[str, dict[str, Any]] = {}
        self._parse_shadow(device_data)

    def _parse_shadow(self, device_data: dict[str, Any]) -> None:
        """Parse shadow properties from device data.

        Supports two schemas:
        - Older: ``reported = {key: value}``
        - Newer (RV2500AX et al.): ``reported = {key: {"value": X, "updatedAt": Y}}``
        """
        shadow = device_data.get("shadow", {})
        props = shadow.get("properties", {})
        reported = props.get("reported", {})

        for key, raw in reported.items():
            if isinstance(raw, dict) and "value" in raw:
                value = raw["value"]
            else:
                value = raw
            self.properties_full[key] = {
                "value": value,
                "read_only": True,
                "base_type": type(value).__name__,
            }

        # Newer device schema renamed several keys. Provide aliases so the
        # vacuum platform's lookups via the sharkiq library Properties enum
        # (e.g. Properties.BATTERY_CAPACITY = "Battery_Capacity") still work.
        aliases = {
            "battery_status": "Battery_Capacity",
            "RechargingToResume": "Recharging_To_Resume",
            "rssi_status": "RSSI",
        }
        for new_key, legacy_key in aliases.items():
            if new_key in self.properties_full and legacy_key not in self.properties_full:
                self.properties_full[legacy_key] = self.properties_full[new_key]

        # Robot_Firmware_Version moved from shadow.reported to registry on
        # newer devices. Surface it through properties_full so sw_version
        # in DeviceInfo still resolves.
        if "Robot_Firmware_Version" not in self.properties_full:
            registry = device_data.get("registry", {})
            fw = registry.get("Robot_Firmware_Version") or registry.get("FW_VERSION")
            if fw:
                self.properties_full["Robot_Firmware_Version"] = {
                    "value": fw,
                    "read_only": True,
                    "base_type": "str",
                }

        # Mark known writable properties
        writable = {
            "Operating_Mode",
            "Power_Mode",
            "Find_Device",
            "Recharge_Resume",
            "Areas_To_Clean",
            "AreasToClean_V2",
            "AreasToClean_V3",
        }
        for key in writable:
            if key in self.properties_full:
                self.properties_full[key]["read_only"] = False

    @property
    def serial_number(self) -> str:
        """Return device serial number (SND)."""
        return self._snd

    @property
    def name(self) -> str:
        """Return device name."""
        return self._name

    @property
    def vac_model_number(self) -> str | None:
        """Return vacuum model number."""
        registry = self._device_data.get("registry", {})
        # Newer schema: registry.modelNumber (top-level) or
        # registry.Device_Model_Number; older schema:
        # registry.metadata.vacModelNumber.
        if (model := registry.get("modelNumber")):
            return model
        if (model := registry.get("Device_Model_Number")):
            return model
        metadata = registry.get("metadata", {})
        if isinstance(metadata, dict):
            return metadata.get("vacModelNumber")
        return None

    @property
    def oem_model_number(self) -> str:
        """Return OEM model number."""
        return self._oem_model or self._model

    @property
    def error_code(self) -> int | None:
        """Return the current error code."""
        return self.get_property_value("Error_Code")

    @property
    def error_text(self) -> str | None:
        """Return human-readable error message."""
        code = self.error_code
        if code is None or code == 0:
            return None
        return ERROR_MESSAGES.get(code, f"Unknown error ({code})")

    def get_property_value(self, property_name: Any) -> Any:
        """Get a property value by name or enum."""
        if isinstance(property_name, enum.Enum):
            property_name = property_name.value

        prop = self.properties_full.get(property_name)
        if prop is None:
            return None
        return prop.get("value")

    async def async_set_property_value(
        self, property_name: Any, value: Any
    ) -> None:
        """Set a property value on the device."""
        if isinstance(property_name, enum.Enum):
            property_name = property_name.value
        if isinstance(value, enum.Enum):
            value = value.value

        await self._api.async_set_device_property(
            self._household_id, self._snd, property_name, value
        )
        # Update local state
        if property_name in self.properties_full:
            self.properties_full[property_name]["value"] = value
        else:
            self.properties_full[property_name] = {
                "value": value,
                "read_only": False,
                "base_type": type(value).__name__,
            }

    async def async_set_operating_mode(self, mode: Any) -> None:
        """Set the operating mode."""
        await self.async_set_property_value("Operating_Mode", mode)

    async def async_find_device(self) -> None:
        """Trigger the find device chirp."""
        await self.async_set_property_value("Find_Device", 1)

    async def async_clean_rooms(
        self, rooms: list[str], clean_type: str = "dry"
    ) -> None:
        """Clean specific rooms.

        Different device generations use different shadow keys:
        - ``AreasToClean_V3``: object form ``{"UserRoom": [...]}``
        - ``AreasToClean_V2``: string-array form ``["UserRoom:Name"]``
        - ``Areas_To_Clean``: legacy string-array form (same payload as V2)

        ``clean_type`` is sent in the V3 payload (the only variant that
        carries it in the shadow) and is otherwise informational. Valid
        values observed in the wild: ``"dry"``, ``"wet"``.
        """
        room_list = self.get_property_value("Robot_Room_List")
        floor_id = ""
        if room_list and ":" in room_list:
            floor_id = room_list.split(":")[0]

        if "AreasToClean_V3" in self.properties_full:
            payload = json.dumps(
                {
                    "areas_to_clean": {"UserRoom": rooms},
                    "clean_count": 1,
                    "floor_id": floor_id,
                    "cleantype": clean_type,
                }
            )
            await self.async_set_property_value("AreasToClean_V3", payload)
        elif "AreasToClean_V2" in self.properties_full:
            areas = [f"UserRoom:{room}" for room in rooms]
            payload = json.dumps(
                {
                    "floor_id": floor_id,
                    "areas_to_clean": areas,
                    "clean_count": 1,
                }
            )
            await self.async_set_property_value("AreasToClean_V2", payload)
        else:
            areas = [f"UserRoom:{room}" for room in rooms]
            payload = json.dumps(
                {
                    "floor_id": floor_id,
                    "areas_to_clean": areas,
                    "clean_count": 1,
                }
            )
            await self.async_set_property_value("Areas_To_Clean", payload)

        # Start cleaning after setting rooms
        await self.async_set_operating_mode(2)  # OperatingModes.START

    async def async_update(
        self, property_list: Any | None = None
    ) -> None:
        """Refresh device state from the API."""
        result = await self._api.async_get_device_state(
            self._household_id, self._snd
        )
        self._device_data = result
        self._parse_shadow(result)
