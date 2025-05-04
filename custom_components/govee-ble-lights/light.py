from __future__ import annotations

import array
import logging
import re
import asyncio

from enum import IntEnum
import bleak_retry_connector

from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ATTR_EFFECT,
    ColorMode,
    LightEntity,
    LightEntityFeature,
    ATTR_COLOR_TEMP_KELVIN
)

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from pathlib import Path
import json
from .govee_utils import prepareMultiplePacketsData
import base64
from . import Hub

_LOGGER = logging.getLogger(__name__)

UUID_CONTROL_CHARACTERISTIC = '00010203-0405-0607-0809-0a0b0c0d2b11'
EFFECT_PARSE = re.compile(r"\[(\d+)/(\d+)/(\d+)/(\d+)]")
SEGMENTED_MODELS = ['H6053', 'H6072', 'H6102', 'H6199', 'H617A', 'H617C']
PERCENT_MODELS = ['H617A']


class LedCommand(IntEnum):
    POWER = 0x01
    BRIGHTNESS = 0x04
    COLOR = 0x05


class LedMode(IntEnum):
    MANUAL = 0x02
    MICROPHONE = 0x06
    SCENES = 0x05
    SEGMENTS = 0x15


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    if config_entry.entry_id in hass.data[DOMAIN]:
        hub: Hub = hass.data[DOMAIN][config_entry.entry_id]
    else:
        return

    if hub.devices is not None:
        devices = hub.devices
        for device in devices:
            if device['type'] == 'devices.types.light':
                _LOGGER.info("Adding device: %s", device)
                async_add_entities([GoveeAPILight(hub, device)])
    elif hub.address is not None:
        ble_device = bluetooth.async_ble_device_from_address(hass, hub.address.upper(), False)
        async_add_entities([GoveeBluetoothLight(hub, ble_device, config_entry)])


class GoveeBluetoothLight(LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature(
        LightEntityFeature.EFFECT | LightEntityFeature.FLASH | LightEntityFeature.TRANSITION)

    def __init__(self, hub: Hub, ble_device, config_entry: ConfigEntry) -> None:
        self._mac = hub.address
        self._model = config_entry.data["model"]
        self._is_segmented = self._model in SEGMENTED_MODELS
        self._use_percent = self._model in PERCENT_MODELS
        self._ble_device = ble_device
        self._state = None
        self._brightness = None
        self._rgb_color = None
        self._effect_list: list[str] | None = None

    async def async_added_to_hass(self):
        self._effect_list = await self._load_effect_list()

    async def _load_effect_list(self) -> list[str]:
        effect_list = []
        json_path = Path(Path(__file__).parent / "jsons" / f"{self._model}.json")
        contents = await asyncio.to_thread(json_path.read_text)
        json_data = json.loads(contents)

        for categoryIdx, category in enumerate(json_data['data']['categories']):
            for sceneIdx, scene in enumerate(category['scenes']):
                for leffectIdx, lightEffect in enumerate(scene['lightEffects']):
                    for seffectIdx, specialEffect in enumerate(lightEffect['specialEffect']):
                        indexes = f"{categoryIdx}/{sceneIdx}/{leffectIdx}/{seffectIdx}"
                        effect_list.append(
                            f"{category['categoryName']} - {scene['sceneName']} - "
                            f"{lightEffect['scenceName']} [{indexes}]"
                        )
        return effect_list

    @property
    def effect_list(self) -> list[str] | None:
        return self._effect_list

    @property
    def name(self) -> str:
        return "GOVEE Light"

    @property
    def unique_id(self) -> str:
        return self._mac.replace(":", "")

    @property
    def brightness(self):
        return self._brightness

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        return self._rgb_color

    @property
    def is_on(self) -> bool | None:
        return self._state

    async def async_turn_on(self, **kwargs) -> None:
        commands = [self._prepareSinglePacketData(LedCommand.POWER, [0x1])]
        self._state = True

        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs.get(ATTR_BRIGHTNESS, 255)
            if self._use_percent:
                brightnessPercent = int(brightness * 100 / 255)
                commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightnessPercent]))
            else:
                commands.append(self._prepareSinglePacketData(LedCommand.BRIGHTNESS, [brightness]))
            self._brightness = brightness

        if ATTR_RGB_COLOR in kwargs:
            red, green, blue = kwargs.get(ATTR_RGB_COLOR)
            if self._is_segmented:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR,
                                                              [LedMode.SEGMENTS, 0x01, red, green, blue, 0x00, 0x00,
                                                               0x00, 0x00, 0x00, 0xFF, 0x7F]))
            else:
                commands.append(self._prepareSinglePacketData(LedCommand.COLOR,
                                                              [LedMode.MANUAL, red, green, blue]))
            self._rgb_color = (red, green, blue)

        if ATTR_EFFECT in kwargs:
            effect = kwargs.get(ATTR_EFFECT)
            if effect:
                search = EFFECT_PARSE.search(effect)
                if search:
                    indexes = [int(search.group(i)) for i in range(1, 5)]

                    json_path = Path(Path(__file__).parent / "jsons" / f"{self._model}.json")
                    contents = await asyncio.to_thread(json_path.read_text)
                    json_data = json.loads(contents)

                    category = json_data['data']['categories'][indexes[0]]
                    scene = category['scenes'][indexes[1]]
                    lightEffect = scene['lightEffects'][indexes[2]]
                    specialEffect = lightEffect['specialEffect'][indexes[3]]

                    for command in prepareMultiplePacketsData(
                            0xa3,
                            array.array('B', [0x02]),
                            array.array('B', base64.b64decode(specialEffect['scenceParam']))):
                        commands.append(command)

        for command in commands:
            client = await self._connectBluetooth()
            await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC, command, False)

    async def async_turn_off(self, **kwargs) -> None:
        client = await self._connectBluetooth()
        await client.write_gatt_char(UUID_CONTROL_CHARACTERISTIC,
                                     self._prepareSinglePacketData(LedCommand.POWER, [0x0]), False)
        self._state = False

    async def _connectBluetooth(self) -> BleakClient:
        for _ in range(3):
            try:
                client = await bleak_retry_connector.establish_connection(
                    BleakClient, self._ble_device, self.unique_id
                )
                return client
            except Exception:
                continue

    def _prepareSinglePacketData(self, cmd, payload):
        if not isinstance(cmd, int):
            raise ValueError('Invalid command')
        if not isinstance(payload, bytes) and not (
                isinstance(payload, list) and all(isinstance(x, int) for x in payload)):
            raise ValueError('Invalid payload')
        if len(payload) > 17:
            raise ValueError('Payload too long')

        cmd = cmd & 0xFF
        payload = bytes(payload)
        frame = bytes([0x33, cmd]) + payload
        frame += bytes([0] * (19 - len(frame)))
        checksum = 0
        for b in frame:
            checksum ^= b
        frame += bytes([checksum & 0xFF])
        return frame
