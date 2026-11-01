"""Application credentials platform for Tidepool (OAuth2 with PKCE).

Endpoints come from the login server's OpenID discovery document; on any
failure the documented production endpoints are used. Neither URLs nor the
discovery body are logged.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final, override

import aiohttp
from homeassistant.components.application_credentials import (
    AuthorizationServer,
    ClientCredential,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    AbstractOAuth2Implementation,
    LocalOAuth2ImplementationWithPkce,
)

from .const import AUTH_URL

_LOGGER = logging.getLogger(__name__)

DISCOVERY_URL: Final = f"{AUTH_URL}/.well-known/openid-configuration"
DOCUMENTED_AUTHORIZE_URL: Final = f"{AUTH_URL}/protocol/openid-connect/auth"
DOCUMENTED_TOKEN_URL: Final = f"{AUTH_URL}/protocol/openid-connect/token"
OAUTH_SCOPES: Final = ("openid", "email", "offline_access")
_DISCOVERY_TIMEOUT: Final = 10


class _DiscoveryError(Exception):
    """The discovery document lacks usable endpoints."""


async def async_get_authorization_server(hass: HomeAssistant) -> AuthorizationServer:
    """Return the login server endpoints, preferring the discovery document."""
    try:
        document = await _async_fetch_discovery(hass)
        authorize_url = _https_url(document, "authorization_endpoint")
        token_url = _https_url(document, "token_endpoint")
    except (aiohttp.ClientError, TimeoutError, ValueError, _DiscoveryError) as err:
        _LOGGER.debug(
            "OpenID discovery failed (%s); using the documented endpoints",
            type(err).__name__,
        )
        return AuthorizationServer(
            authorize_url=DOCUMENTED_AUTHORIZE_URL, token_url=DOCUMENTED_TOKEN_URL
        )
    return AuthorizationServer(authorize_url=authorize_url, token_url=token_url)


async def async_get_auth_implementation(
    hass: HomeAssistant, auth_domain: str, credential: ClientCredential
) -> AbstractOAuth2Implementation:
    """Return a PKCE implementation; the client secret may be empty."""
    server = await async_get_authorization_server(hass)
    return TidepoolOAuth2Implementation(
        hass,
        auth_domain,
        credential.client_id,
        authorize_url=server.authorize_url,
        token_url=server.token_url,
        client_secret=credential.client_secret,
    )


class TidepoolOAuth2Implementation(LocalOAuth2ImplementationWithPkce):
    """Authorization code flow with PKCE S256 and Tidepool's scopes."""

    @property
    @override
    def extra_authorize_data(self) -> dict:
        """Add the scopes to the PKCE parameters."""
        return {"scope": " ".join(OAUTH_SCOPES), **super().extra_authorize_data}


async def _async_fetch_discovery(hass: HomeAssistant) -> dict[str, Any]:
    session = async_get_clientsession(hass)
    async with asyncio.timeout(_DISCOVERY_TIMEOUT):
        async with session.get(DISCOVERY_URL) as response:
            response.raise_for_status()
            document = await response.json(content_type=None)
    if not isinstance(document, dict):
        raise _DiscoveryError("discovery document is not an object")
    return document


def _https_url(document: dict[str, Any], key: str) -> str:
    value = document.get(key)
    if not isinstance(value, str) or not value.startswith("https://"):
        raise _DiscoveryError(f"discovery document lacks an https {key}")
    return value
