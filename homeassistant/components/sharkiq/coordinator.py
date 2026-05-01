"""Data update coordinator for shark iq vacuums."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sharkiq import (
    AylaApi,
    SharkIqAuthError,
    SharkIqAuthExpiringError,
    SharkIqNotAuthedError,
    SharkIqVacuum,
)

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .auth import SharkAuthError, SharkAuthExpiredError
from .const import (
    API_TIMEOUT,
    AVAILABILITY_GRACE,
    DOMAIN,
    LOGGER,
    UPDATE_INTERVAL,
    UPDATE_INTERVAL_IDLE,
)
from .skegox import SkegoxApi, SkegoxApiError, SkegoxDevice

# Union type for devices from either backend
type SharkDevice = SharkIqVacuum | SkegoxDevice

type SharkIqConfigEntry = ConfigEntry[SharkIqUpdateCoordinator]


class SharkIqUpdateCoordinator(DataUpdateCoordinator[bool]):
    """Define a wrapper class to update Shark IQ data."""

    config_entry: SharkIqConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: SharkIqConfigEntry,
        shark_vacs: list[SharkDevice],
        ayla_api: AylaApi | None = None,
        skegox_api: SkegoxApi | None = None,
    ) -> None:
        """Set up the SharkIqUpdateCoordinator class."""
        self.ayla_api = ayla_api
        self.skegox_api = skegox_api
        self.shark_vacs: dict[str, SharkDevice] = {
            vac.serial_number: vac for vac in shark_vacs
        }
        self._online_dsns: set[str] = set()
        # Timestamp of the most recent successful refresh. Lets entities
        # apply a grace period to availability so transient SharkNinja /
        # network failures don't flap the entity unavailable.
        self.last_update_success_time: datetime | None = None
        # Per-device "selected rooms" queue and clean mode used by the
        # composer entities (room switches + clean-mode select + start
        # button). The switch and select entities seed these from
        # RestoreEntity on startup; the start button reads them.
        self.room_queue: dict[str, set[str]] = {
            vac.serial_number: set() for vac in shark_vacs
        }
        self.clean_type: dict[str, str] = {
            vac.serial_number: "dry" for vac in shark_vacs
        }

        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )

    @property
    def is_skegox(self) -> bool:
        """Return True if using the skegox backend."""
        return self.skegox_api is not None

    @property
    def has_recent_success(self) -> bool:
        """Return True if the last successful refresh is within the grace window.

        Use this in entity ``available`` properties instead of
        ``last_update_success`` directly so transient poll failures don't
        flap the entity into the unavailable state immediately.
        """
        if self.last_update_success:
            return True
        if self.last_update_success_time is None:
            return False
        return (
            datetime.now(timezone.utc) - self.last_update_success_time
            < AVAILABILITY_GRACE
        )

    @property
    def online_dsns(self) -> set[str]:
        """Get the set of all online DSNs."""
        return self._online_dsns

    def device_is_online(self, dsn: str) -> bool:
        """Return the online state of a given vacuum dsn."""
        return dsn in self._online_dsns

    @staticmethod
    async def _async_update_vacuum(sharkiq: SharkDevice) -> None:
        """Asynchronously update the data for a single vacuum."""
        dsn = sharkiq.serial_number
        LOGGER.debug("Updating sharkiq data for device DSN %s", dsn)
        async with asyncio.timeout(API_TIMEOUT):
            await sharkiq.async_update()

    def _adjust_update_interval(self) -> None:
        """Tighten or loosen the poll cadence based on whether any vacuum is active.

        Vacuums spend the vast majority of their lives docked and idle. Polling
        every 30 s while nothing is happening burns API quota and host CPU for
        no benefit. We use the fast cadence whenever any tracked vacuum is
        cleaning, paused, or returning to base, and back off to the idle cadence
        otherwise. Property reads here use string keys so the logic works for
        both Ayla SharkIqVacuum and SkegoxDevice without importing the sharkiq
        Properties enum.
        """
        active = False
        for vac in self.shark_vacs.values():
            op_mode = vac.get_property_value("Operating_Mode")
            charging = vac.get_property_value("Charging_Status")
            # Active = anything other than docked-and-idle. op_mode==0 (STOP)
            # combined with Charging_Status truthy means "sitting on the dock".
            if charging and (op_mode in (None, 0)):
                continue
            active = True
            break

        target = UPDATE_INTERVAL if active else UPDATE_INTERVAL_IDLE
        if self.update_interval != target:
            LOGGER.debug(
                "Switching sharkiq poll interval to %s (active=%s)", target, active
            )
            self.update_interval = target

    async def _async_update_data(self) -> bool:
        """Update data device by device."""
        if self.is_skegox:
            result = await self._async_update_skegox()
        else:
            result = await self._async_update_ayla()
        self.last_update_success_time = datetime.now(timezone.utc)
        self._adjust_update_interval()
        return result

    async def _async_update_ayla(self) -> bool:
        """Update via the Ayla backend."""
        assert self.ayla_api is not None
        try:
            if (
                self.ayla_api.token_expiring_soon
                or datetime.now()
                > self.ayla_api.auth_expiration - timedelta(seconds=600)
            ):
                await self.ayla_api.async_refresh_auth()

            all_vacuums = await self.ayla_api.async_list_devices()
            self._online_dsns = {
                v["dsn"]
                for v in all_vacuums
                if v["connection_status"] == "Online" and v["dsn"] in self.shark_vacs
            }

            LOGGER.debug("Updating sharkiq data (Ayla)")
            online_vacs = (self.shark_vacs[dsn] for dsn in self.online_dsns)
            await asyncio.gather(*(self._async_update_vacuum(v) for v in online_vacs))
        except (
            SharkIqAuthError,
            SharkIqNotAuthedError,
            SharkIqAuthExpiringError,
        ) as err:
            LOGGER.debug("Bad auth state (Ayla). Attempting re-auth", exc_info=err)
            raise ConfigEntryAuthFailed from err
        except Exception as err:
            LOGGER.exception("Unexpected error updating SharkIQ (Ayla)")
            raise UpdateFailed(err) from err

        return True

    async def _async_update_skegox(self) -> bool:
        """Update via the skegox backend."""
        assert self.skegox_api is not None
        try:
            # Refresh auth if needed
            if self.skegox_api.auth.token_expiring_soon:
                await self.skegox_api.auth.async_refresh_auth()

            # Get device connection status
            all_vacuums = await self.skegox_api.async_list_devices()
            self._online_dsns = {
                v["dsn"]
                for v in all_vacuums
                if v["connection_status"] == "Online" and v["dsn"] in self.shark_vacs
            }

            LOGGER.debug("Updating sharkiq data (Skegox)")
            online_vacs = (self.shark_vacs[dsn] for dsn in self.online_dsns)
            await asyncio.gather(*(self._async_update_vacuum(v) for v in online_vacs))
        except (SharkAuthError, SharkAuthExpiredError) as err:
            LOGGER.debug("Bad auth state (Skegox). Attempting re-auth", exc_info=err)
            raise ConfigEntryAuthFailed from err
        except SkegoxApiError as err:
            LOGGER.exception("Error updating SharkIQ (Skegox)")
            raise UpdateFailed(err) from err
        except Exception as err:
            LOGGER.exception("Unexpected error updating SharkIQ (Skegox)")
            raise UpdateFailed(err) from err

        return True
