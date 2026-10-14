"""Home Assistant setup-only entity selection (skipped in BLE-only tests)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

pytest.importorskip("homeassistant")

import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.exceptions import ConfigEntryNotReady

from custom_components.dohm import async_setup_entry, async_unload_entry
from custom_components.dohm.config_flow import DohmConfigFlow, DohmOptionsFlow
from custom_components.dohm.const import CONF_ENTITY_TYPE


@pytest.fixture
def entry():
    return SimpleNamespace(
        data={"address": "AA:BB:CC:01:36:C4"},
        options={},
        entry_id="dohm_entry",
        unique_id="AA:BB:CC:01:36:C4",
        title="Bedroom Dohm",
    )


@pytest.fixture
def hass(entry):
    return SimpleNamespace(
        config_entries=SimpleNamespace(
            async_get_known_entry=Mock(return_value=entry),
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        )
    )


@pytest.fixture
def flow(hass):
    flow = DohmConfigFlow()
    flow.hass = hass
    flow._address = "AA:BB:CC:01:36:C4"
    flow._suggested_name = "MARPAC_DOHM"
    return flow


async def test_initial_form_defaults_to_media_player_and_validates_choices(flow):
    result = await flow.async_step_name()
    schema = result["data_schema"]
    assert schema({}) == {"name": "MARPAC_DOHM", CONF_ENTITY_TYPE: "media_player"}
    for selected in ("fan", "media_player"):
        assert schema({CONF_ENTITY_TYPE: selected})[CONF_ENTITY_TYPE] == selected
    with pytest.raises(vol.Invalid):
        schema({CONF_ENTITY_TYPE: "invalid"})


@pytest.mark.parametrize("selected", ["fan", "media_player", None])
async def test_initial_form_stores_type_in_data_not_options(flow, selected):
    user_input = {"name": "  Bedroom Dohm  ", "area": "bedroom"}
    if selected is not None:
        user_input[CONF_ENTITY_TYPE] = selected
    result = await flow.async_step_name(user_input)
    assert result["title"] == "Bedroom Dohm"
    assert result["data"] == {
        "address": "AA:BB:CC:01:36:C4",
        "area": "bedroom",
        CONF_ENTITY_TYPE: selected or "media_player",
    }
    assert not result.get("options")


async def test_blank_name_and_absent_area_preserve_setup_choice(flow):
    result = await flow.async_step_name({"name": " ", CONF_ENTITY_TYPE: "fan"})
    assert result["title"] == "MARPAC_DOHM"
    assert result["data"] == {"address": flow._address, CONF_ENTITY_TYPE: "fan"}


async def test_options_remains_rebond_only(entry, hass):
    flow = DohmOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    menu = await flow.async_step_init()
    assert menu["menu_options"] == ["rebond"]
    assert not hasattr(flow, "async_step_entity_type")


@pytest.mark.parametrize(
    "selected,expected",
    [
        (None, Platform.MEDIA_PLAYER),
        ("media_player", Platform.MEDIA_PLAYER),
        ("fan", Platform.FAN),
        ("invalid", Platform.MEDIA_PLAYER),
    ],
)
async def test_setup_only_selected_platform(
    monkeypatch, entry, hass, selected, expected
):
    if selected is not None:
        entry.data[CONF_ENTITY_TYPE] = selected
    # Setup selection is never taken from options.
    entry.options = {
        CONF_ENTITY_TYPE: "fan" if expected == Platform.MEDIA_PLAYER else "media_player"
    }
    coordinator = SimpleNamespace(async_config_entry_first_refresh=AsyncMock())
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_ble_device_from_address",
        Mock(return_value=object()),
    )
    monkeypatch.setattr("custom_components.dohm.client.DohmClient", Mock())
    monkeypatch.setattr(
        "custom_components.dohm.coordinator.DohmCoordinator",
        Mock(return_value=coordinator),
    )
    assert await async_setup_entry(hass, entry)
    coordinator.async_config_entry_first_refresh.assert_awaited_once()
    assert coordinator.loaded_platform == expected
    assert entry.runtime_data is coordinator
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, [expected]
    )


async def test_setup_missing_device_forwards_no_platform(monkeypatch, entry, hass):
    monkeypatch.setattr(
        "homeassistant.components.bluetooth.async_ble_device_from_address",
        Mock(return_value=None),
    )
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_entry(hass, entry)
    hass.config_entries.async_forward_entry_setups.assert_not_awaited()


@pytest.mark.parametrize("loaded", [Platform.MEDIA_PLAYER, Platform.FAN])
@pytest.mark.parametrize("success", [True, False])
async def test_unload_selected_platform_and_disconnect_only_on_success(
    entry, hass, loaded, success
):
    entry.runtime_data = SimpleNamespace(
        loaded_platform=loaded, client=SimpleNamespace(disconnect=AsyncMock())
    )
    hass.config_entries.async_unload_platforms.return_value = success
    assert await async_unload_entry(hass, entry) is success
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, [loaded])
    assert entry.runtime_data.client.disconnect.await_count == int(success)
