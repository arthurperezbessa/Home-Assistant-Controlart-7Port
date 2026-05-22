"""Plataforma cover (cortina/persiana) da integração ControlArt 7Port.

Cada cortina é um subentry da entrada do 7Port. A entidade:

- Envia comandos IR de abrir, fechar e parar via 7Port.
- Rastreia o estado de forma otimista (não há feedback de posição via IR).
- Bloqueia o fechamento quando um sensor de janela indica que a janela
  atrelada à cortina está aberta.
- Restaura o último estado após reinício do Home Assistant.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.cover import (
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.const import STATE_CLOSED, STATE_OPEN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import SevenPortConfigEntry
from .const import (
    CMD_CLOSE,
    CMD_OPEN,
    CMD_STOP,
    CONF_DEVICE_ID,
    CONF_DEVICE_TYPE,
    CONF_IR_PORT,
    CONF_WINDOW_SENSOR,
    DEVICE_TYPE_COVER,
    DOMAIN,
)
from .device_db import DeviceDefinition, async_get_database
from .tcp import SevenPortClient, SevenPortError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SevenPortConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Cria as entidades cover a partir dos subentries da entrada."""
    database = await async_get_database(hass)
    client = entry.runtime_data.client

    for subentry_id, subentry in entry.subentries.items():
        if subentry.data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_COVER:
            continue
        definition = database.get(subentry.data.get(CONF_DEVICE_ID, ""))
        if definition is None:
            _LOGGER.warning(
                "Subentry '%s' referencia uma definição de cortina inexistente (%s); ignorado.",
                subentry.title,
                subentry.data.get(CONF_DEVICE_ID),
            )
            continue

        entity = SevenPortCover(
            entry_id=entry.entry_id,
            subentry_id=subentry_id,
            name=subentry.title,
            options=dict(subentry.data),
            definition=definition,
            client=client,
        )
        async_add_entities([entity], config_subentry_id=subentry_id)


class SevenPortCover(CoverEntity, RestoreEntity):
    """Entidade de cortina/persiana controlada via IR pelo 7Port."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_device_class = CoverDeviceClass.CURTAIN

    def __init__(
        self,
        *,
        entry_id: str,
        subentry_id: str,
        name: str,
        options: dict[str, Any],
        definition: DeviceDefinition,
        client: SevenPortClient,
    ) -> None:
        """Inicializa a entidade."""
        self._entry_id = entry_id
        self._definition = definition
        self._client = client
        self._ir_port: int = int(options[CONF_IR_PORT])
        self._window_sensor_id: str | None = options.get(CONF_WINDOW_SENSOR) or None

        self._attr_unique_id = f"{subentry_id}_cover"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=name,
            manufacturer=definition.brand,
            model=definition.model,
            via_device=(DOMAIN, entry_id),
        )

        # Estado inicial desconhecido — RestoreEntity preenche no async_added_to_hass.
        self._is_closed: bool | None = None

        # Features: open + close sempre; stop apenas se houver código para ele.
        features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE
        if definition.command(CMD_STOP):
            features |= CoverEntityFeature.STOP
        self._attr_supported_features = features

    # -- Ciclo de vida --------------------------------------------------------

    async def async_added_to_hass(self) -> None:
        """Restaura o último estado conhecido após reinício do HA."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            if last.state == STATE_CLOSED:
                self._is_closed = True
            elif last.state in (STATE_OPEN, "opening", "closing"):
                self._is_closed = False
            # Qualquer outro estado (unknown, unavailable) → mantém None.

    # -- Propriedades ---------------------------------------------------------

    @property
    def is_closed(self) -> bool | None:
        """Retorna True se fechada, False se aberta, None se posição desconhecida."""
        return self._is_closed

    # -- Comandos -------------------------------------------------------------

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Abre a cortina via IR."""
        await self._async_send_ir_cmd(CMD_OPEN)
        self._is_closed = False
        self.async_write_ha_state()

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Fecha a cortina via IR.

        O comando é bloqueado se o sensor de janela configurado indicar que
        a janela está aberta (estado 'on' do binary_sensor).
        """
        if self._window_sensor_id:
            sensor = self.hass.states.get(self._window_sensor_id)
            if sensor is not None and sensor.state == "on":
                _LOGGER.warning(
                    "Fechamento da cortina '%s' bloqueado: sensor de janela '%s' "
                    "indica que a janela está aberta.",
                    self.name,
                    self._window_sensor_id,
                )
                return

        await self._async_send_ir_cmd(CMD_CLOSE)
        self._is_closed = True
        self.async_write_ha_state()

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Para o movimento da cortina via IR."""
        await self._async_send_ir_cmd(CMD_STOP)
        self.async_write_ha_state()

    # -- Utilitários ----------------------------------------------------------

    async def _async_send_ir_cmd(self, cmd: str) -> None:
        """Envia um código IR de comando para a porta configurada."""
        code = self._definition.command(cmd)
        if not code:
            _LOGGER.warning(
                "Cortina '%s' não tem código IR para o comando '%s'.",
                self.name,
                cmd,
            )
            return
        try:
            await self._client.async_send_ir(self._ir_port, code)
        except SevenPortError as err:
            _LOGGER.error(
                "Falha ao enviar IR para cortina '%s' (porta %s): %s",
                self.name,
                self._ir_port,
                err,
            )
