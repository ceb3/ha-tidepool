"""The Tidepool integration."""

from __future__ import annotations

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    async_get_config_entry_implementation,
)

from .api import AuthStrategy, TidepoolClient
from .auth import LegacyAuth, OAuthAuth
from .const import AUTH_METHOD_LEGACY, CONF_AUTH_METHOD, CONF_PATIENTS
from .coordinator import TidepoolConfigEntry, TidepoolFastCoordinator, TidepoolRuntime

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: TidepoolConfigEntry) -> bool:
    """Set up Tidepool from a config entry."""
    client = TidepoolClient(
        async_get_clientsession(hass), await _async_auth_strategy(hass, entry)
    )
    runtime = TidepoolRuntime(client=client, fast={})
    # Coordinators read the shared auth flag from the runtime while refreshing.
    entry.runtime_data = runtime

    patients: list[str] = entry.options.get(CONF_PATIENTS, entry.data[CONF_PATIENTS])
    for user_id in patients:
        coordinator = TidepoolFastCoordinator(hass, entry, client, user_id)
        runtime.fast[user_id] = coordinator
        await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TidepoolConfigEntry) -> bool:
    """Unload a config entry; its coordinators shut down with it."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_auth_strategy(
    hass: HomeAssistant, entry: TidepoolConfigEntry
) -> AuthStrategy:
    """Create the entry's one auth strategy, shared by all its coordinators."""
    session = async_get_clientsession(hass)
    if entry.data[CONF_AUTH_METHOD] == AUTH_METHOD_LEGACY:
        return LegacyAuth(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD])
    implementation = await async_get_config_entry_implementation(hass, entry)
    return OAuthAuth(hass, entry, implementation)
