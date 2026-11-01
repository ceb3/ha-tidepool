"""The Tidepool integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import (
    AuthError,
    AuthStrategy,
    NotFoundError,
    TidepoolClient,
    TidepoolError,
)
from .auth import PasswordAuth
from .const import CONF_PATIENTS, DEFAULT_DEVICE_NAME, DOMAIN
from .coordinator import (
    TidepoolConfigEntry,
    TidepoolFastCoordinator,
    TidepoolRuntime,
    TidepoolSlowCoordinator,
    async_clear_login_withdrawn,
)
from .devices import async_setup_devices, identifiers_in_use
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.EVENT, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the services, which belong to the integration, not to an entry."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TidepoolConfigEntry) -> bool:
    """Set up Tidepool from a config entry."""
    client = TidepoolClient(async_get_clientsession(hass), _auth_strategy(hass, entry))
    runtime = TidepoolRuntime(client=client, fast={}, slow={})
    # Coordinators read the shared auth flag from the runtime while refreshing.
    entry.runtime_data = runtime

    patients: list[str] = entry.options.get(CONF_PATIENTS, entry.data[CONF_PATIENTS])
    for user_id in patients:
        coordinator = TidepoolFastCoordinator(hass, entry, client, user_id)
        runtime.fast[user_id] = coordinator
        await coordinator.async_config_entry_first_refresh()

    # A patient without a name has no entities, so needs no slow data either.
    for user_id in patients:
        await _async_name_patient(runtime, user_id)
        if user_id in runtime.names:
            slow = TidepoolSlowCoordinator(hass, entry, client, user_id)
            runtime.slow[user_id] = slow
            await slow.async_config_entry_first_refresh()

    async_setup_devices(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TidepoolConfigEntry) -> bool:
    """Unload a config entry; its coordinators shut down with it.

    The repair issue goes too: the next setup raises it again if the sign-in
    is still withdrawn.
    """
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        async_clear_login_withdrawn(hass, entry.entry_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: TidepoolConfigEntry) -> None:
    """Delete the repair issue of a removed entry.

    An entry that never loaded (setup retrying) is not unloaded through
    ``async_unload_entry``, so its issue would outlive it.
    """
    async_clear_login_withdrawn(hass, entry.entry_id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: TidepoolConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow removing a child device that the current data no longer shows.

    Patient devices and the devices behind the latest records stay; a removed
    device in use would only be registered again on the next refresh.
    """
    return not device.identifiers & identifiers_in_use(entry)


async def _async_name_patient(runtime: TidepoolRuntime, user_id: str) -> None:
    """Name a patient's device after the profile; fall back if it has no name.

    The name ends up in entity ids, so a passing failure must retry the whole
    setup rather than register a fallback. A missing profile (404) or a blank
    name is a stable result. A refused profile is not an entry-level auth
    failure: that patient is skipped and the others carry on.
    """
    try:
        name = (await runtime.client.profile(user_id)).get("fullName")
    except NotFoundError:
        name = None
    except AuthError:
        _LOGGER.warning("Skipping a patient whose profile Tidepool refused to share")
        return
    except TidepoolError as err:
        raise ConfigEntryNotReady("could not read a patient profile") from err
    if isinstance(name, str) and name.strip():
        runtime.names[user_id] = name.strip()
    else:
        runtime.names[user_id] = DEFAULT_DEVICE_NAME


def _auth_strategy(hass: HomeAssistant, entry: TidepoolConfigEntry) -> AuthStrategy:
    """Create the entry's one auth strategy, shared by all its coordinators.

    Entries made by older versions may carry an ``auth_method`` key; it is
    ignored, since email and password is the only sign-in.
    """
    return PasswordAuth(
        async_get_clientsession(hass),
        entry.data[CONF_EMAIL],
        entry.data[CONF_PASSWORD],
    )
