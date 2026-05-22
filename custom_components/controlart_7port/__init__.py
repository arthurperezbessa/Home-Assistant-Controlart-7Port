"""Integração ControlArt 7Port para o Home Assistant.

Cria uma entrada de configuração (config entry) por equipamento 7Port.
Cada aparelho controlado (ar-condicionado, TV, etc.) é um subentry,
representado como um device com suas próprias entidades.
"""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_HOST, CONF_PORT, DOMAIN
from .device_db import async_get_database
from .tcp import SevenPortClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.BINARY_SENSOR, Platform.CLIMATE, Platform.MEDIA_PLAYER]

type SevenPortConfigEntry = ConfigEntry["SevenPortRuntimeData"]


class SevenPortRuntimeData:
    """Dados em memória de uma entrada de configuração 7Port."""

    def __init__(self, client: SevenPortClient) -> None:
        """Inicializa os dados de runtime."""
        self.client = client


async def async_setup_entry(
    hass: HomeAssistant, entry: SevenPortConfigEntry
) -> bool:
    """Configura uma entrada (um equipamento 7Port)."""
    # Garante que o banco de dispositivos esteja carregado.
    await async_get_database(hass)

    client = SevenPortClient(entry.data[CONF_HOST], entry.data[CONF_PORT])
    entry.runtime_data = SevenPortRuntimeData(client)

    # Registra a própria 7Port como um device "hub".
    device_registry = dr.async_get(hass)
    device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        manufacturer="ControlArt",
        name=entry.title,
        model="7Port",
        configuration_url=f"http://{entry.data[CONF_HOST]}",
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: SevenPortConfigEntry
) -> bool:
    """Descarrega uma entrada de configuração."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_update_listener(
    hass: HomeAssistant, entry: SevenPortConfigEntry
) -> None:
    """Recarrega a entrada quando opções ou subentries mudam.

    Adicionar/editar/remover um aparelho (subentry) dispara este listener,
    fazendo as plataformas reavaliarem a lista de dispositivos.
    """
    await hass.config_entries.async_reload(entry.entry_id)
