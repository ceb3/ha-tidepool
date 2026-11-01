"""Config flow for Tidepool: OAuth or legacy sign-in, patients, reauth, options.

Email, password, tokens and user ids never appear in logs, error text or
description placeholders. A patient label may show the account's display name.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, override

import aiohttp
import probatio
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.config_entry_oauth2_flow import (
    AbstractOAuth2FlowHandler,
)
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import AuthError, TidepoolClient, TidepoolError
from .auth import (
    LegacyAuth,
    LegacyLoginWithdrawnError,
    oauth_token_headers,
    token_user_id,
)
from .const import (
    AUTH_METHOD_LEGACY,
    AUTH_METHOD_OAUTH,
    CONF_AUTH_METHOD,
    CONF_FAST_INTERVAL,
    CONF_PATIENTS,
    DEFAULT_FAST_INTERVAL,
    DOMAIN,
    LEGACY_DISCLAIMER,
    MAX_FAST_INTERVAL,
    MIN_FAST_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_TITLE = "Tidepool"

_EMAIL_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
)
_PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


class TidepoolConfigFlow(AbstractOAuth2FlowHandler, domain=DOMAIN):
    """Set up a Tidepool login with OAuth (recommended) or email and password."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._data: dict[str, Any] = {}
        self._patient_options: list[SelectOptionDict] = []
        self._title = DEFAULT_TITLE

    @property
    @override
    def logger(self) -> logging.Logger:
        """Return the logger of this flow."""
        return _LOGGER

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> TidepoolOptionsFlow:
        """Return the options flow."""
        return TidepoolOptionsFlow()

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user pick the sign-in method."""
        return self.async_show_menu(
            step_id="user", menu_options=[AUTH_METHOD_OAUTH, AUTH_METHOD_LEGACY]
        )

    async def async_step_oauth(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in with OAuth through the application credentials."""
        return await self.async_step_pick_implementation()

    async def async_step_legacy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Sign in with email and password (legacy, shows the disclaimer)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            session = async_get_clientsession(self.hass)
            auth = LegacyAuth(session, email, password)
            user_id, error = await _async_legacy_login(auth)
            if user_id is None:
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_id)
                self._abort_if_unique_id_configured()
                error = await self._async_load_patients(
                    TidepoolClient(session, auth), user_id
                )
                if error is not None:
                    errors["base"] = error
                else:
                    self._data = {
                        CONF_AUTH_METHOD: AUTH_METHOD_LEGACY,
                        CONF_EMAIL: email,
                        CONF_PASSWORD: password,
                    }
                    return await self.async_step_patients()

        schema = probatio.Schema(
            {
                probatio.Required(CONF_EMAIL): _EMAIL_SELECTOR,
                probatio.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
            }
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(
                schema, {CONF_EMAIL: user_input[CONF_EMAIL]}
            )
        return self.async_show_form(
            step_id="legacy",
            data_schema=schema,
            errors=errors,
            description_placeholders={"disclaimer": LEGACY_DISCLAIMER},
        )

    @override
    async def async_oauth_create_entry(self, data: dict) -> ConfigFlowResult:
        """Identify the login from the token, then pick patients or reauth."""
        token = data["token"]
        user_id = token_user_id(token)
        if user_id is None:
            return self.async_abort(reason="no_user_id")

        await self.async_set_unique_id(user_id)
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="wrong_account")
            return self.async_update_reload_and_abort(
                self._get_reauth_entry(), data_updates=data
            )
        self._abort_if_unique_id_configured()

        session = async_get_clientsession(self.hass)
        client = TidepoolClient(session, _TokenAuth(token["access_token"]))
        if (error := await self._async_load_patients(client, user_id)) is not None:
            return self.async_abort(reason=error)
        self._data = {CONF_AUTH_METHOD: AUTH_METHOD_OAUTH, **data}
        return await self.async_step_patients()

    async def async_step_patients(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Choose which accounts (patients) to add; the login's own by default."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if patients := list(user_input[CONF_PATIENTS]):
                return self.async_create_entry(
                    title=self._title, data={**self._data, CONF_PATIENTS: patients}
                )
            errors["base"] = "no_patients"

        assert self.unique_id is not None
        return self.async_show_form(
            step_id="patients",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_PATIENTS, default=[self.unique_id]
                    ): _patients_selector(self._patient_options)
                }
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauthentication after the credentials stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new password (legacy) or sign in again (OAuth)."""
        entry = self._get_reauth_entry()
        if entry.data.get(CONF_AUTH_METHOD) == AUTH_METHOD_LEGACY:
            return await self._async_reauth_legacy(entry, user_input)
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_pick_implementation(
            {"implementation": entry.data["auth_implementation"]}
        )

    async def _async_reauth_legacy(
        self, entry: ConfigEntry, user_input: dict[str, Any] | None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            auth = LegacyAuth(
                async_get_clientsession(self.hass), entry.data[CONF_EMAIL], password
            )
            user_id, error = await _async_legacy_login(auth)
            if user_id is None:
                errors["base"] = error
            else:
                await self.async_set_unique_id(user_id)
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: password}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=probatio.Schema(
                {probatio.Required(CONF_PASSWORD): _PASSWORD_SELECTOR}
            ),
            errors=errors,
        )

    async def _async_load_patients(
        self, client: TidepoolClient, user_id: str
    ) -> str | None:
        """Load the patient choices and the title; return an error key."""
        try:
            self._patient_options = await _async_patient_options(client, user_id)
        except Exception as err:
            return _error_key(err)
        own_label = self._patient_options[0]["label"]
        self._title = own_label if own_label != user_id else DEFAULT_TITLE
        return None


class TidepoolOptionsFlow(OptionsFlowWithReload):
    """Change the selected patients and the fast refresh interval."""

    def __init__(self) -> None:
        self._patient_options: list[SelectOptionDict] | None = None

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and save the options; the entry reloads when they change."""
        entry = self.config_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            if patients := list(user_input[CONF_PATIENTS]):
                return self.async_create_entry(
                    data={
                        CONF_PATIENTS: patients,
                        CONF_FAST_INTERVAL: int(user_input[CONF_FAST_INTERVAL]),
                    }
                )
            errors["base"] = "no_patients"

        selected: list[str] = list(
            entry.options.get(CONF_PATIENTS, entry.data.get(CONF_PATIENTS, []))
        )
        if self._patient_options is None:
            self._patient_options = await self._async_patient_options(selected)
        interval = entry.options.get(CONF_FAST_INTERVAL, DEFAULT_FAST_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=probatio.Schema(
                {
                    probatio.Required(
                        CONF_PATIENTS, default=selected
                    ): _patients_selector(self._patient_options),
                    probatio.Required(
                        CONF_FAST_INTERVAL, default=interval
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=MIN_FAST_INTERVAL,
                            max=MAX_FAST_INTERVAL,
                            step=1,
                            mode=NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        )
                    ),
                }
            ),
            errors=errors,
        )

    async def _async_patient_options(
        self, selected: list[str]
    ) -> list[SelectOptionDict]:
        """Load the accounts; offline, offer only the selected ones."""
        entry = self.config_entry
        options: list[SelectOptionDict] = []
        client = _options_client(self.hass, entry)
        try:
            if client is not None:
                assert entry.unique_id is not None
                options = await _async_patient_options(client, entry.unique_id)
        except Exception as err:
            _LOGGER.debug(
                "Could not load the patient list (%s); offering the selected ones",
                type(err).__name__,
            )
        known = {option["value"] for option in options}
        options.extend(
            SelectOptionDict(value=account, label=account)
            for account in selected
            if account not in known
        )
        return options


class _TokenAuth:
    """Auth from a just-issued OAuth access token, used only inside the flow."""

    def __init__(self, access_token: str) -> None:
        self._access_token = access_token

    def __repr__(self) -> str:
        return f"<{type(self).__name__}>"

    async def async_headers(self) -> dict[str, str]:
        return oauth_token_headers(self._access_token)

    async def async_invalidate(self) -> None:
        """Nothing to refresh within the flow."""


def _loaded_client(entry: ConfigEntry) -> TidepoolClient | None:
    """Return the loaded entry's shared client (``runtime_data.client``)."""
    if entry.state is not ConfigEntryState.LOADED:
        return None
    return getattr(getattr(entry, "runtime_data", None), "client", None)


def _options_client(hass: HomeAssistant, entry: ConfigEntry) -> TidepoolClient | None:
    """Return a client for the options flow, or ``None`` to stay offline.

    Prefers the loaded entry's shared client. An OAuth entry never gets a
    second ``OAuthAuth``: two refreshers of one rotating refresh token race,
    and the loser's ``invalid_grant`` would start a needless reauth.
    """
    if (client := _loaded_client(entry)) is not None:
        return client
    if entry.data.get(CONF_AUTH_METHOD) != AUTH_METHOD_LEGACY:
        return None
    session = async_get_clientsession(hass)
    return TidepoolClient(
        session,
        LegacyAuth(session, entry.data[CONF_EMAIL], entry.data[CONF_PASSWORD]),
    )


async def _async_legacy_login(auth: LegacyAuth) -> tuple[str | None, str]:
    """Log in once; return the user id, or ``None`` and an error key."""
    try:
        await auth.async_headers()
    except Exception as err:
        return None, _error_key(err)
    if auth.user_id is None:
        return None, "legacy_withdrawn"
    return auth.user_id, ""


async def _async_patient_options(
    client: TidepoolClient, user_id: str
) -> list[SelectOptionDict]:
    """List the login's own account first, then the accounts shared with it.

    Labels are the profile's full name when available, else the account id.
    """
    groups = await client.access_groups(user_id)
    accounts = [user_id, *(account for account in groups if account != user_id)]
    options: list[SelectOptionDict] = []
    for account in accounts:
        try:
            name = (await client.profile(account)).get("fullName")
        except TidepoolError:
            name = None
        label = name.strip() if isinstance(name, str) and name.strip() else account
        options.append(SelectOptionDict(value=account, label=label))
    return options


def _patients_selector(options: list[SelectOptionDict]) -> SelectSelector:
    return SelectSelector(
        SelectSelectorConfig(
            options=options, multiple=True, mode=SelectSelectorMode.LIST
        )
    )


def _error_key(err: Exception) -> str:
    """Map a sign-in or API failure to a form error key (never logs details)."""
    if isinstance(err, LegacyLoginWithdrawnError):
        return "legacy_withdrawn"
    if isinstance(err, AuthError):
        return "invalid_auth"
    if isinstance(err, TidepoolError | aiohttp.ClientError | TimeoutError):
        return "cannot_connect"
    _LOGGER.error("Unexpected error while signing in (%s)", type(err).__name__)
    return "unknown"
