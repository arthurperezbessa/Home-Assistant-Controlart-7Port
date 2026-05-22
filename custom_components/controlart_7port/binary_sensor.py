"""Plataforma binary_sensor da integração ControlArt 7Port.

Cria um sensor de conectividade (ping TCP) para cada equipamento 7Port,
verificando periodicamente se o dispositivo está acessível na rede.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SevenPortConfigEntry
from .const import DOMAIN
from .tcp import SevenPortClient

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SevenPortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Cria o sensor de conectividade para a 7Port."""
    client = entry.runtime_data.client
    async_add_entities(
        [SevenPortConnectivity(entry.entry_id, client)],
        update_before_add=True,
    )


class SevenPortConnectivity(BinarySensorEntity):
    """Sensor de conectividade TCP da 7Port."""

    _attr_has_entity_name = True
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_should_poll = True
    _attr_translation_key = "connectivity"

    def __init__(self, entry_id: str, client: SevenPortClient) -> None:
        """Inicializa o sensor."""
        self._client = client
        self._attr_unique_id = f"{entry_id}_connectivity"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
        )

    async def async_update(self) -> None:
        """Testa a conexão TCP com a 7Port e atualiza o estado."""
        self._attr_is_on = await self._client.async_test_connection()
