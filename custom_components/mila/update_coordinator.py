"""Data update coordinator for Mila Air Purifiers"""

import asyncio
import async_timeout
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_SCAN_INTERVAL, CONF_TIMEOUT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from milasdk import MilaApi, MilaError, OAuthError

from .auth import MilaConfigEntryAuth, MilaOauthImplementation
from .const import (
    DATAKEY_ACCOUNT,
    DATAKEY_APPLIANCE,
    DATAKEY_LOCATION,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DOMAIN
)
from .devices import MilaDevice, MilaAppliance, MilaLocation

PLATFORMS = ["sensor","switch","fan","select"]
_LOGGER = logging.getLogger(__name__)

class MilaUpdateCoordinator(DataUpdateCoordinator):
    """Define a wrapper class to update Mila API data."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Set up the MilaUpdateCoordinator class."""
        self._hass = hass
        self._config_entry = config_entry        
        self._api = None  # Will be initialized in async_setup

        options = config_entry.options
        self._update_interval = options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        self._timeout = options.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)
        self._initialized = False
        self.devices: dict[str, MilaDevice] = {}

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=self._update_interval))

    async def async_setup(self):
        """Setup a new coordinator"""
        _LOGGER.debug("Setting up coordinator")
        
        # Initialize API here to avoid blocking call in __init__
        # Use asyncio.to_thread to move the blocking file read operation to a thread
        try:
            self._api = await asyncio.to_thread(
                lambda: MilaApi(MilaConfigEntryAuth(self._hass, self._config_entry, MilaOauthImplementation(self._hass, self._config_entry)))
            )
        except Exception as e:
            _LOGGER.error(f"Failed to initialize Mila API: {e}")
            return False

        _LOGGER.debug("Getting first refresh")
        await self.async_config_entry_first_refresh()
        self._initialized = True

        _LOGGER.debug("Forwarding setup to platforms")
        await self.hass.config_entries.async_forward_entry_setups(
            self._config_entry,
            PLATFORMS
        )

        return True

    async def async_reset(self):
        """Resets the coordinator."""
        _LOGGER.debug("resetting the coordinator")

        unload_ok = await self.hass.config_entries.async_unload_platforms(
            self._config_entry, 
            PLATFORMS
        )
        return unload_ok

    async def _async_update_data(self):
        """Fetch data from API endpoint.

        This is the place to pre-process the data to lookup tables
        so entities can quickly look up their data.
        """
        try:
            data = {}

            #get the list of known appliances
            existing_appliances: list[str] = self.data.get(DATAKEY_APPLIANCE).keys() if self.data is not None else []
            
            # Check if API is initialized
            if self._api is None:
                raise UpdateFailed("Mila API not initialized")

            #only need to get the account info the first time
            if not self._initialized:
                async with async_timeout.timeout(self._timeout):
                    data[DATAKEY_ACCOUNT] = await self._api.get_account()

            async with async_timeout.timeout(self._timeout):
                data[DATAKEY_APPLIANCE] = {x["id"]: x for x in await self._api.get_appliances()}
            async with async_timeout.timeout(self._timeout):
                data[DATAKEY_LOCATION] = {f"loc_{x['id']}": x for x in await self._api.get_location_data()}

            #build the device list if needed
            if not self._initialized:
                await self._build_devices(data)
            else:
                #detect new devices and notify the user
                await self._detect_new_devices(existing_appliances, data[DATAKEY_APPLIANCE])

            return data
        except (OAuthError) as ex:
            raise ConfigEntryAuthFailed from ex            
        except MilaError as err:
            raise UpdateFailed(f"Error communicating with API: {err}")

    async def _build_devices(self, data: dict[str,Any]):
        if self._api is None:
            _LOGGER.error("Cannot build devices: Mila API not initialized")
            return
            
        for id in data[DATAKEY_APPLIANCE].keys():
            _LOGGER.info(f"Found Mila device with id={id}, setting up...")
            self.devices[id] = MilaAppliance(self, self._api, id)
        for id in data[DATAKEY_LOCATION].keys():
            _LOGGER.info(f"Found Mila location with id={id}, setting up...")
            self.devices[id] = MilaLocation(self, self._api, id)

    async def _detect_new_devices(self, old: list[str], new: dict[str,Any]):
        diff = set(new)-set(old)
        for id in diff:
            _LOGGER.info(
                f"New device with id={id} detected, reload the Mila integration if you want to access it in Home Assistant"
            )
