"""Config flow for the Moncton Water integration."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
)

from .api import MonctonWaterClient, build_ssl_context
from .const import (
    BASE_URL,
    CONF_BACKFILL_DAILY,
    CONF_SCAN_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .exceptions import MonctonWaterAuthError, MonctonWaterError

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

_LOGGER = logging.getLogger(__name__)


async def _validate_login(hass: HomeAssistant, username: str, password: str) -> str:
    """Log in to the portal end-to-end; return the account number.

    Runs the full bootstrap: form login and an account-page fetch.
    Raises ValueError("invalid_auth"|"cannot_connect") on failure.
    """
    client = MonctonWaterClient(
        async_get_clientsession(hass),
        base_url=BASE_URL,
        # CA-bundle loading touches the filesystem; keep it off the loop.
        ssl_context=await hass.async_add_executor_job(build_ssl_context),
    )
    try:
        account = await client.bootstrap(username, password)
    except MonctonWaterAuthError as err:
        _LOGGER.warning("Moncton Water sign-in rejected: %s", err)
        raise ValueError("invalid_auth") from err
    except (MonctonWaterError, TimeoutError, aiohttp.ClientError) as err:
        # Surface the underlying cause in the log — the form only says
        # "cannot connect", which covers everything from a timeout to an
        # unexpected page layout.
        _LOGGER.warning(
            "Moncton Water sign-in failed: %s: %s", type(err).__name__, err,
            exc_info=err,
        )
        raise ValueError("cannot_connect") from err
    return account.account_number


class MonctonWaterConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Moncton Water config flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            account_number = None
            try:
                account_number = await _validate_login(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except ValueError as err:
                errors["base"] = str(err.args[0])
            if account_number:
                await self.async_set_unique_id(f"account_{account_number}")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"Moncton Water {account_number}",
                    data=user_input,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Handle re-authentication when stored credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        existing_entry = self._get_reauth_entry()
        if user_input is not None:
            try:
                await _validate_login(
                    self.hass,
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                )
            except ValueError as err:
                errors["base"] = str(err.args[0])
            if not errors:
                return self.async_update_reload_and_abort(
                    existing_entry, data=user_input
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=existing_entry.data.get(CONF_USERNAME, ""),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    def async_get_options_flow(config_entry) -> MonctonWaterOptionsFlow:
        """Create the options flow."""
        return MonctonWaterOptionsFlow()


class MonctonWaterOptionsFlow(OptionsFlow):
    """Allow tweaking the polling interval and backfill behavior."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=round(
                            options.get(
                                CONF_SCAN_INTERVAL,
                                DEFAULT_SCAN_INTERVAL.total_seconds() / 3600,
                            )
                        ),
                    ): NumberSelector(
                        NumberSelectorConfig(
                            min=1, max=24, step=1, mode=NumberSelectorMode.BOX
                        )
                    ),
                    vol.Required(
                        CONF_BACKFILL_DAILY,
                        default=options.get(CONF_BACKFILL_DAILY, True),
                    ): bool,
                }
            ),
        )
