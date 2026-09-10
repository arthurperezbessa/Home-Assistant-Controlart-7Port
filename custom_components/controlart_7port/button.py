"""Plataforma button da integração ControlArt 7Port.

Expõe o giro horizontal dos flaps motorizados de TV. Cada sentido vira um
botão porque o Home Assistant não representa movimento lateral em um cover:
as setas de um cover são sempre cima/baixo, e reaproveitá-las para esquerda/
direita indicaria a direção errada na interface.

Botões não guardam estado, o que combina com RF: não há feedback de posição.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import SevenPortConfigEntry
from .const import (
    CMD_LEFT,
    CMD_RIGHT,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_IR_PORT,
    DEVICE_TYPE_FLAP,
    DOMAIN,
)
from .device_db import DeviceDefinition, async_get_database
from .tcp import SevenPortClient, SevenPortError

_LOGGER = logging.getLogger(__name__)

# Comando → chave de tradução do nome da entidade.
_FLAP_BUTTONS = {
    CMD_LEFT: "flap_left",
    CMD_RIGHT: "flap_right",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SevenPortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Cria as entidades button a partir dos subentries da entrada."""
    database = await async_get_database(hass)
    client = entry.runtime_data.client

    for subentry_id, subentry in entry.subentries.items():
        if subentry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_FLAP:
            continue
        definition = database.get(subentry.data.get(CONF_DEVICE_ID, ""))
        if definition is None:
            continue

        # Só cria o botão de um sentido se a definição tiver o código dele.
        entities = [
            SevenPortFlapButton(
                entry_id=entry.entry_id,
                subentry_id=subentry_id,
                name=subentry.title,
                options=dict(subentry.data),
                definition=definition,
                client=client,
                command=cmd,
                translation_key=key,
            )
            for cmd, key in _FLAP_BUTTONS.items()
            if definition.command(cmd)
        ]
        if entities:
            async_add_entities(entities, config_subentry_id=subentry_id)


class SevenPortFlapButton(ButtonEntity):
    """Botão de giro horizontal de um flap motorizado de TV."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        entry_id: str,
        subentry_id: str,
        name: str,
        options: dict[str, Any],
        definition: DeviceDefinition,
        client: SevenPortClient,
        command: str,
        translation_key: str,
    ) -> None:
        """Inicializa a entidade."""
        self._definition = definition
        self._client = client
        self._command = command
        self._ir_port: int = int(options[CONF_IR_PORT])

        self._attr_translation_key = translation_key
        self._attr_unique_id = f"{subentry_id}_{command}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=name,
            manufacturer=definition.brand,
            model=definition.model,
            via_device=(DOMAIN, entry_id),
        )

    async def async_press(self) -> None:
        """Envia o código do sentido correspondente."""
        code = self._definition.command(self._command)
        if not code:
            return
        try:
            await self._client.async_send_code(self._ir_port, code)
        except SevenPortError as err:
            _LOGGER.error(
                "Falha ao enviar '%s' para '%s': %s",
                self._command,
                self.name,
                err,
            )
