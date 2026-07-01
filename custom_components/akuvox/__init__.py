"""Custom integration to integrate akuvox with Home Assistant.

For more details about this integration, please refer to
https://github.com/nimroddolev/akuvox
"""
from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .config_flow import AkuvoxOptionsFlowHandler
from .api import AkuvoxApiClient
from .const import (
    DOMAIN,
    LOGGER
)
from .coordinator import AkuvoxDataUpdateCoordinator

PLATFORMS: list[Platform] = [
    Platform.CAMERA,
    Platform.BUTTON,
    Platform.SENSOR
]

# https://developers.home-assistant.io/docs/config_entries_index/#setting-up-an-entry
async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up this integration using UI."""
    hass.data.setdefault(DOMAIN, {})
    client = AkuvoxApiClient(
        session=async_get_clientsession(hass),
        hass=hass,
        entry=entry,
    )
    hass.data[DOMAIN][entry.entry_id] = coordinator = AkuvoxDataUpdateCoordinator(
        hass=hass,
        client=client,
    )
    await async_update_configuration(hass=hass, entry=entry)
    await client.async_load_auth_session()
    if not await client.async_ensure_token_valid():
        raise ConfigEntryAuthFailed(
            "Akuvox token refresh was rejected; new app tokens are required"
        )

    # https://developers.home-assistant.io/docs/integration_fetching_data#coordinated-single-api-poll-for-data-for-all-entities
    await coordinator.async_config_entry_first_refresh()

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    await client.async_start_token_refresh_scheduler()
    _async_register_services(hass)

    return True


def _async_register_services(hass: HomeAssistant) -> None:
    """Register integration services once."""
    if hass.services.has_service(DOMAIN, "refresh_tokens"):
        return

    async def async_refresh_tokens(call: ServiceCall) -> None:
        """Force a controlled token rotation for testing or recovery."""
        entry_id = call.data["entry_id"]
        coordinator = hass.data.get(DOMAIN, {}).get(entry_id)
        if coordinator is None:
            raise HomeAssistantError(
                f"Akuvox config entry {entry_id} is not loaded"
            )
        if not await coordinator.client.async_refresh_token(force=True):
            raise HomeAssistantError("Akuvox rejected the token refresh")

    hass.services.async_register(
        DOMAIN,
        "refresh_tokens",
        async_refresh_tokens,
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Handle removal of an entry."""
    api_client: AkuvoxApiClient = hass.data[DOMAIN][entry.entry_id].client
    await api_client.async_stop_token_refresh_scheduler()
    await api_client.async_stop_polling()
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await hass.config_entries.async_reload(entry.entry_id)

# Polling

async def async_stop_polling(hass: HomeAssistant):
    """Stop polling the personal door log API."""
    api_client: AkuvoxApiClient = get_api_client(hass=hass) # type: ignore
    await api_client.async_stop_polling()

async def async_start_polling(hass: HomeAssistant):
    """Stop polling the personal door log API."""
    api_client: AkuvoxApiClient = get_api_client(hass=hass) # type: ignore
    await api_client.async_start_polling_personal_door_log()

def get_api_client(hass: HomeAssistant):
    """Akuvox API Client."""
    for _key, value in hass.data[DOMAIN].items():
        coordinator: AkuvoxDataUpdateCoordinator = value
        return coordinator.client

# Integration options

async def async_options(self, entry: ConfigEntry):
    """Present current configuration options for modification."""
    # Create an options flow handler and return it
    return AkuvoxOptionsFlowHandler(entry)

async def async_options_updated(self, entry: ConfigEntry):
    """Handle updated configuration options and update the entry."""
    LOGGER.debug("Updated option keys: %s", sorted(entry.options))

# Update

async def async_update_configuration(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update stored values from configuration."""
    try:
        if entry.options:
            updated_options: dict = entry.options.copy()

            # Wait for image URL?
            updated_options["wait_for_image_url"] = bool(updated_options.get("event_screenshot_options", "") == "wait")

            # Update API & data classes
            coordinator: AkuvoxDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id]
            client: AkuvoxApiClient = coordinator.client

            LOGGER.debug("Configured values:")
            for key, value in updated_options.items():
                #                           value=value)
                if value:
                    client.update_data(key, value)
                    str_value: str = str(value)
                    if key in ["auth_token", "token", "refresh_token"]:
                        str_value = "<redacted>"
                    LOGGER.debug(" - %s = %s", key, str_value)
    except Exception as error:
        LOGGER.warning("Unable to update configuration: %s", str(error))
