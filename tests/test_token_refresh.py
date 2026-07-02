"""Tests for Akuvox rotating token handling."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol

from custom_components.akuvox.api import AkuvoxApiClient
from custom_components.akuvox.button import AkuvoxDoorRelayEntity
from custom_components.akuvox.config_flow import AkuvoxFlowHandler
from custom_components.akuvox.config_flow import AkuvoxOptionsFlowHandler
from custom_components.akuvox.data import AkuvoxData


@pytest.fixture
def client() -> AkuvoxApiClient:
    """Create a client focused on the refresh lifecycle."""
    api_client = object.__new__(AkuvoxApiClient)
    api_client._refresh_lock = asyncio.Lock()
    api_client._data = SimpleNamespace(
        token="access-old-secret",
        refresh_token="refresh-old-secret",
        token_expires_at=0,
        subdomain="ecloud",
        host="",
    )

    async def save_session(token, refresh_token, expires_at):
        api_client._data.token = token
        api_client._data.refresh_token = refresh_token
        api_client._data.token_expires_at = expires_at

    api_client._data.async_save_auth_session = AsyncMock(
        side_effect=save_session
    )
    return api_client


@pytest.mark.asyncio
async def test_refresh_rotates_and_persists_complete_pair(
    client, monkeypatch, caplog
):
    """A successful response replaces both credentials in one write."""
    monkeypatch.setattr(
        "custom_components.akuvox.api.time.time", lambda: 1_000
    )
    client._async_api_wrapper = AsyncMock(
        return_value={
            "err_code": "0",
            "message": "success",
            "datas": {
                "token": "access-new-secret",
                "refresh_token": "refresh-new-secret",
                "token_valid": "604800",
            },
        }
    )

    assert await client.async_refresh_token(force=True)

    request = client._async_api_wrapper.await_args.kwargs
    assert request["url"] == (
        "https://gate.subdomain.akuvox.com:8600/refresh_token"
    )
    assert request["headers"]["x-auth-token"] == "access-old-secret"
    assert request["headers"]["api-version"] == "6.8"
    assert json.loads(request["data"]) == {
        "refresh_token": "refresh-old-secret"
    }
    client._data.async_save_auth_session.assert_awaited_once_with(
        "access-new-secret",
        "refresh-new-secret",
        605_800,
    )
    assert client._data.token == "access-new-secret"
    assert client._data.refresh_token == "refresh-new-secret"
    assert "access-old-secret" not in caplog.text
    assert "refresh-old-secret" not in caplog.text
    assert "access-new-secret" not in caplog.text
    assert "refresh-new-secret" not in caplog.text


@pytest.mark.asyncio
async def test_refresh_rejection_preserves_existing_pair(client):
    """A rejected refresh never partially updates the saved session."""
    client._async_api_wrapper = AsyncMock(
        return_value={
            "err_code": "1000100003",
            "message": "refreshToken error.",
        }
    )

    assert not await client.async_refresh_token(force=True)
    client._data.async_save_auth_session.assert_not_awaited()
    assert client._data.token == "access-old-secret"
    assert client._data.refresh_token == "refresh-old-secret"


@pytest.mark.asyncio
async def test_config_flow_rotation_does_not_create_orphan_auth_storage(
    client,
):
    """Pre-entry validation updates memory without writing a shared store."""
    client._async_api_wrapper = AsyncMock(
        return_value={
            "err_code": "0",
            "datas": {
                "token": "access-new-secret",
                "refresh_token": "refresh-new-secret",
                "token_valid": "604800",
            },
        }
    )

    assert await client.async_refresh_token(force=True, persist=False)
    client._data.async_save_auth_session.assert_not_awaited()
    assert client._data.token == "access-new-secret"
    assert client._data.refresh_token == "refresh-new-secret"


@pytest.mark.asyncio
async def test_concurrent_refresh_uses_rotating_token_once(client):
    """Concurrent callers cannot consume the same refresh token twice."""
    request_started = asyncio.Event()
    release_response = asyncio.Event()

    async def refresh_response(**_kwargs):
        request_started.set()
        await release_response.wait()
        return {
            "err_code": "0",
            "datas": {
                "token": "access-new-secret",
                "refresh_token": "refresh-new-secret",
                "token_valid": "604800",
            },
        }

    client._async_api_wrapper = AsyncMock(side_effect=refresh_response)

    first = asyncio.create_task(client.async_refresh_token(force=True))
    await request_started.wait()
    second = asyncio.create_task(client.async_refresh_token(force=True))
    await asyncio.sleep(0)
    release_response.set()

    assert await first
    assert await second
    client._async_api_wrapper.assert_awaited_once()
    client._data.async_save_auth_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_valid_token_skips_early_rotation(client, monkeypatch):
    """A token outside its safety window is not needlessly rotated."""
    monkeypatch.setattr(
        "custom_components.akuvox.api.time.time", lambda: 1_000
    )
    client._data.token_expires_at = 200_000
    client._async_api_wrapper = AsyncMock()

    assert await client.async_ensure_token_valid()
    client._async_api_wrapper.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_polling_is_idempotent(client):
    """Repeated API initialization cannot create duplicate poll loops."""
    existing_poller = SimpleNamespace(is_polling=True)
    client.door_log_poller = existing_poller

    await client.async_start_polling()

    assert client.door_log_poller is existing_poller


@pytest.mark.asyncio
async def test_refresh_scheduler_is_a_background_task(client):
    """The long-lived scheduler must not block Home Assistant startup."""

    class FakeHass:
        task = None
        name = None

        def async_create_background_task(self, target, name):
            self.task = target
            self.name = name
            return SimpleNamespace(done=lambda: False)

    client.hass = FakeHass()
    client._refresh_task = None

    await client.async_start_token_refresh_scheduler()

    assert client.hass.name == "Akuvox token refresh scheduler"
    client.hass.task.close()


@pytest.mark.asyncio
async def test_door_log_rate_limit_increases_backoff(client, monkeypatch):
    """HTTP 429 responses slow polling instead of hammering Akuvox."""
    client._last_response_status = 429
    client._door_log_poll_interval = 30
    client._door_log_backoff = 30
    client.async_get_personal_door_log = AsyncMock(return_value=None)
    sleep = AsyncMock(side_effect=asyncio.CancelledError)
    monkeypatch.setattr("custom_components.akuvox.api.asyncio.sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await client.async_retrieve_personal_door_log()

    sleep.assert_awaited_once_with(60)
    assert client._door_log_backoff == 60


def test_token_query_values_are_redacted_from_log_urls():
    """API URLs must never expose the rotating access token in logs."""
    assert AkuvoxApiClient._redact_url(
        "https://rest.example/userconf?token=super-secret&other=1"
    ) == (
        "https://rest.example/userconf?token=<redacted>&other=1"
    )


def test_credentials_are_redacted_from_logged_payloads():
    """Unexpected API responses cannot leak any credential field."""
    assert AkuvoxApiClient._redact_payload(
        {
            "message": "failed",
            "token": "access-secret",
            "datas": {
                "refresh_token": "refresh-secret",
                "password": "password-secret",
            },
        }
    ) == {
        "message": "failed",
        "token": "<redacted>",
        "datas": {
            "refresh_token": "<redacted>",
            "password": "<redacted>",
        },
    }


@pytest.mark.asyncio
async def test_door_press_uses_latest_rotated_token():
    """Door entities resolve the live client token instead of caching it."""
    open_door = Mock()

    class FakeHass:
        async def async_add_executor_job(self, target, *args):
            return target(*args)

    entity = object.__new__(AkuvoxDoorRelayEntity)
    entity._client = SimpleNamespace(
        hass=FakeHass(),
        _data=SimpleNamespace(
            host="rest.example:8443",
            token="latest-rotated-token",
        ),
        make_opendoor_request=open_door,
    )
    entity._name = "Gate, 1"
    entity._data = "mac=123&relay=1"

    await entity.async_press()

    open_door.assert_called_once_with(
        "Gate, 1",
        "rest.example:8443",
        "latest-rotated-token",
        "mac=123&relay=1",
    )


@pytest.mark.asyncio
async def test_auth_session_is_saved_as_one_complete_record():
    """The rotating pair cannot be split across independent storage writes."""
    data = object.__new__(AkuvoxData)
    data._auth_storage_lock = asyncio.Lock()
    data._auth_store = SimpleNamespace(async_save=AsyncMock())

    await data.async_save_auth_session(
        "access-new",
        "refresh-new",
        123456,
    )

    data._auth_store.async_save.assert_awaited_once_with(
        {
            "token": "access-new",
            "refresh_token": "refresh-new",
            "token_expires_at": 123456,
        }
    )
    assert data.token == "access-new"
    assert data.refresh_token == "refresh-new"


def test_app_token_setup_requires_refresh_token():
    """Token-based setup cannot create a non-renewable session."""
    flow = object.__new__(AkuvoxFlowHandler)
    flow.hass = SimpleNamespace(config=SimpleNamespace(country="GB"))
    schema = vol.Schema(flow.get_app_tokens_sign_in_schema())
    input_data = {
        "country_code": "United Kingdom",
        "phone_number": "7700900000",
        "auth_token": "auth",
        "token": "access",
        "subdomain": "ecloud",
    }

    with pytest.raises(vol.Invalid):
        schema(input_data)

    input_data["refresh_token"] = "refresh"
    assert schema(input_data)["refresh_token"] == "refresh"


def test_options_flow_uses_supported_config_entry_storage():
    """Options flow initialization must not assign a read-only HA property."""
    entry = SimpleNamespace(options={}, data={})

    flow = AkuvoxOptionsFlowHandler(entry)

    assert flow.config_entry is entry
