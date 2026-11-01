"""The Tidepool integration."""

from __future__ import annotations

import logging

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    AuthError,
    AuthStrategy,
    NotFoundError,
    TidepoolClient,
    TidepoolError,
)
from .auth import PasswordAuth
from .const import CONF_PATIENTS, DEFAULT_DEVICE_NAME
from .coordinator import (
    TidepoolConfigEntry,
    TidepoolFastCoordinator,
    TidepoolRuntime,
    TidepoolSlowCoordinator,
)
from .devices import async_setup_devices, identifiers_in_use

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.SENSOR]


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
    """Unload a config entry; its coordinators shut down with it."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


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
